import collections
import traceback
import urlparse
import logging
import urllib
import socket
import select
import struct
import heapq
import fcntl
import cffi
import time
import sys
import os


from greenlet import getcurrent
from greenlet import greenlet


__version__ = '0.0.1'


log = logging.getLogger(__name__)


class Timeout(Exception):
    pass


class Closed(Exception):
    pass


class Stop(Closed):
    pass


class Filter(Exception):
    pass


class Reraise(Exception):
    pass


class preserve_exception(object):
    """
    Marker to pass exceptions through channels
    """
    def __init__(self):
        self.typ, self.val, self.tb = sys.exc_info()

    def reraise(self):
        try:
            raise Reraise('Unhandled exception')
        except:
            traceback.print_exc()
            sys.stderr.write('\nOriginal exception -->\n\n')
            raise self.typ, self.val, self.tb


def ospipe():
    """creates an os pipe and sets it up for async io"""
    pipe_r, pipe_w = os.pipe()
    flags = fcntl.fcntl(pipe_w, fcntl.F_GETFL, 0)
    flags = flags | os.O_NONBLOCK
    fcntl.fcntl(pipe_w, fcntl.F_SETFL, flags)
    return pipe_r, pipe_w


def init_C():
    ffi = cffi.FFI()

    ffi.cdef("""
        ssize_t read(int fd, void *buf, size_t count);

        int eventfd(unsigned int initval, int flags);

        #define SIG_BLOCK ...
        #define SIG_UNBLOCK ...
        #define SIG_SETMASK ...

        typedef struct { ...; } sigset_t;

        int sigprocmask(int how, const sigset_t *set, sigset_t *oldset);

        int sigemptyset(sigset_t *set);
        int sigfillset(sigset_t *set);
        int sigaddset(sigset_t *set, int signum);
        int sigdelset(sigset_t *set, int signum);
        int sigismember(const sigset_t *set, int signum);

        #define SFD_NONBLOCK ...
        #define SFD_CLOEXEC ...

        #define EAGAIN ...

        #define SIGALRM ...
        #define SIGINT ...
        #define SIGTERM ...

        struct signalfd_siginfo {
            uint32_t ssi_signo;   /* Signal number */
            ...;
        };

        int signalfd(int fd, const sigset_t *mask, int flags);
    """)

    C = ffi.verify("""
        #include <unistd.h>
        #include <sys/eventfd.h>
        #include <sys/signalfd.h>
        #include <signal.h>
    """)

    # stash some conveniences on C
    C.ffi = ffi
    C.NULL = ffi.NULL

    def Cdot(f):
        setattr(C, f.__name__, f)

    @Cdot
    def sigset(*nums):
        s = ffi.new("sigset_t *")
        assert not C.sigemptyset(s)

        for num in nums:
            rc = C.sigaddset(s, num)
            assert not rc, "signum: %s doesn't specify a valid signal." % num
        return s

    return C


C = init_C()


class Event(object):
    """
    An event object manages an internal flag that can be set to true with the
    set() method and reset to false with the clear() method. The wait() method
    blocks until the flag is true.
    """

    __slots__ = ['hub', 'fired', 'waiters']

    def __init__(self, hub, fired=False):
        self.hub = hub
        self.fired = fired
        self.waiters = collections.deque()

    def __nonzero__(self):
        return self.fired

    def wait(self):
        if self.fired:
            return
        self.waiters.append(getcurrent())
        self.hub.pause()

    def set(self):
        self.fired = True
        # isolate this group of waiters in case of a clear
        waiters = self.waiters
        while waiters:
            waiter = waiters.popleft()
            self.hub.switch_to(waiter)

    def clear(self):
        self.fired = False
        # start a new list of waiters, which will block until the next set
        self.waiters = collections.deque()
        return self


class Channel(object):

    __slots__ = ['hub', 'closed', 'pipeline', 'items', 'waiters']

    def __init__(self, hub):
        self.hub = hub
        self.closed = False
        self.pipeline = None
        self.items = collections.deque()
        self.waiters = collections.deque()

    def __call__(self, f):
        if not self.pipeline:
            self.pipeline = []
        self.pipeline.append(f)

    def send(self, item):
        if self.closed:
            raise Closed

        if self.pipeline and not isinstance(item, Closed):
            try:
                for f in self.pipeline:
                    item = f(item)
            except Filter:
                return
            except Exception, e:
                item = e

        if not self.waiters:
            self.items.append(item)
            return

        getter = self.waiters.popleft()
        if isinstance(item, Exception):
            self.hub.throw_to(getter, item)
        else:
            self.hub.switch_to(getter, (self, item))

    def recv(self, timeout=-1):
        if self.items:
            item = self.items.popleft()
            if isinstance(item, preserve_exception):
                item.reraise()
            if isinstance(item, Exception):
                raise item
            return item

        if timeout == 0:
            raise Timeout('timeout: %s' % timeout)

        self.waiters.append(getcurrent())
        try:
            item = self.hub.pause(timeout=timeout)
            ch, item = item
        except Timeout:
            self.waiters.remove(getcurrent())
            raise
        return item

    def throw(self):
        self.send(preserve_exception())

    def __iter__(self):
        while True:
            try:
                yield self.recv()
            except Closed:
                raise StopIteration

    def close(self):
        self.send(Closed("closed"))
        self.closed = True


class Signal(object):
    def __init__(self, hub):
        self.hub = hub
        self.fd = -1
        self.count = 0
        self.mapper = {}
        self.reverse_mapper = {}

    def start(self, fd):
        self.fd = fd

        info = C.ffi.new("struct signalfd_siginfo *")
        size = C.ffi.sizeof("struct signalfd_siginfo")

        ready = self.hub.register(fd, select.EPOLLIN)

        @self.hub.spawn
        def _():
            while True:
                try:
                    fd, event = ready.recv()
                except Closed:
                    self.stop()
                    return

                rc = C.read(fd, info, size)
                assert rc == size

                num = info.ssi_signo
                for ch in self.mapper[num]:
                    ch.send(num)

    def stop(self):
        if self.fd == -1:
            return

        fd = self.fd
        self.fd = -1
        self.count = 0
        self.mapper = {}
        self.reverse_mapper = {}

        self.hub.unregister(fd)
        os.close(fd)

    def reset(self):
        if self.count == len(self.mapper):
            return

        self.count = len(self.mapper)

        if not self.count:
            self.stop()
            return

        mask = C.sigset(*self.mapper.keys())
        rc = C.sigprocmask(C.SIG_SETMASK, mask, C.NULL)
        assert not rc
        fd = C.signalfd(self.fd, mask, C.SFD_NONBLOCK | C.SFD_CLOEXEC)

        if self.fd == -1:
            self.start(fd)

    def subscribe(self, *signals):
        out = self.hub.channel()
        self.reverse_mapper[out] = signals
        for num in signals:
            self.mapper.setdefault(num, []).append(out)
        self.reset()
        return out

    def unsubscribe(self, ch):
        for num in self.reverse_mapper[ch]:
            self.mapper[num].remove(ch)
            if not self.mapper[num]:
                del self.mapper[num]
        del self.reverse_mapper[ch]
        self.reset()


class Hub(object):
    def __init__(self):
        self.ready = collections.deque()
        self.scheduled = Scheduler()
        self.stopped = self.event()

        self.epoll = select.epoll()
        self.registered = {}

        self.signal = Signal(self)
        self.tcp = TCP(self)
        self.http = HTTP(self)

        self.loop = greenlet(self.main)

    def event(self, fired=False):
        return Event(self, fired)

    def channel(self):
        return Channel(self)

    def pause(self, timeout=-1):
        if timeout > -1:
            item = self.scheduled.add(
                timeout, getcurrent(), Timeout('timeout: %s' % timeout))

        resume = self.loop.switch()

        if timeout > -1:
            if isinstance(resume, Timeout):
                raise resume

            # since we didn't timeout, remove ourselves from scheduled
            self.scheduled.remove(item)

        # TODO: clean up stopped handling here
        if self.stopped:
            raise Closed("closed")

        return resume

    def switch_to(self, target, *a):
        self.ready.append((getcurrent(), ()))
        return target.switch(*a)

    def throw_to(self, target, *a):
        self.ready.append((getcurrent(), ()))
        if len(a) == 1 and isinstance(a[0], preserve_exception):
            return target.throw(a[0].typ, a[0].val, a[0].tb)
        return target.throw(*a)

    def spawn(self, f, *a):
        self.ready.append((f, a))

    def spawn_later(self, ms, f, *a):
        self.scheduled.add(ms, f, *a)

    def sleep(self, ms=1):
        self.scheduled.add(ms, getcurrent())
        self.loop.switch()

    def register(self, fd, mask):
        self.registered[fd] = self.channel()
        self.epoll.register(fd, mask)
        return self.registered[fd]

    def unregister(self, fd):
        if fd in self.registered:
            self.epoll.unregister(fd)
            self.registered[fd].close()
            del self.registered[fd]

    def stop(self):
        self.sleep(1)

        for fd, ch in self.registered.items():
            ch.send(Stop('stop'))

        while self.scheduled:
            task, a = self.scheduled.pop()
            self.throw_to(task, Stop('stop'))

        try:
            self.stopped.wait()
        except Closed:
            return

    def stop_on_term(self):
        done = self.signal.subscribe(C.SIGINT, C.SIGTERM)
        done.recv()
        self.stop()

    def main(self):
        """
        Scheduler steps:
            - run ready until exhaustion

            - if there's something scheduled
                - run overdue scheduled immediately
                - or if there's nothing registered, sleep until next scheduled
                  and then go back to ready

            - if there's nothing registered and nothing scheduled, we've
              deadlocked, so stopped

            - epoll on registered, with timeout of next scheduled, if something
              is scheduled
        """
        def run_task(task, *a):
            if isinstance(task, greenlet):
                task.switch(*a)
            else:
                greenlet(task).switch(*a)

        while True:
            while self.ready:
                task, a = self.ready.popleft()
                run_task(task, *a)

            if self.scheduled:
                timeout = self.scheduled.timeout()
                # run overdue scheduled immediately
                if timeout < 0:
                    task, a = self.scheduled.pop()
                    run_task(task, *a)
                    continue

                # if nothing registered, just sleep until next scheduled
                if not self.registered:
                    time.sleep(timeout)
                    task, a = self.scheduled.pop()
                    run_task(task, *a)
                    continue
            else:
                timeout = -1

            # TODO: add better handling for deadlock
            if not self.registered:
                self.stopped.set()
                return

            # run epoll
            events = None
            while True:
                try:
                    events = self.epoll.poll(timeout=timeout)
                    break
                # ignore IOError from signal interrupts
                except IOError:
                    continue

            if not events:
                # timeout
                task, a = self.scheduled.pop()
                run_task(task, *a)

            else:
                for fd, event in events:
                    if fd in self.registered:
                        self.registered[fd].send((fd, event))


class Scheduler(object):
    Item = collections.namedtuple('Item', ['due', 'action', 'args'])

    def __init__(self):
        self.count = 0
        self.queue = []
        self.removed = {}

    def add(self, delay, action, *args):
        due = time.time() + (delay / 1000.0)
        item = self.Item(due, action, args)
        heapq.heappush(self.queue, item)
        self.count += 1
        return item

    def __len__(self):
        return self.count

    def remove(self, item):
        self.removed[item] = True
        self.count -= 1

    def prune(self):
        while True:
            if self.queue[0] not in self.removed:
                break
            item = heapq.heappop(self.queue)
            del self.removed[item]

    def timeout(self):
        self.prune()
        return self.queue[0].due - time.time()

    def pop(self):
        self.prune()
        item = heapq.heappop(self.queue)
        self.count -= 1
        return item.action, item.args


# TCP ######################################################################


class TCP(object):
    def __init__(self, hub):
        self.hub = hub

    def listen(self, port=0, host='127.0.0.1'):
        return TCPListener(self.hub, host, port)

    def connect(self, port, host='127.0.0.1'):
        return TCPConn.connect(self.hub, host, port)


"""
struct.pack reference
uint32: "I"
uint64: 'Q"

Packet
    type|size: uint32 (I)
        type (2 bits):
            PUSH    = 0
            REQUEST = 1
            REPLY   = 2
            OP      = 3
        size (30 bits, 1GB)    # for type PUSH/REQUEST/REPLY
        or OPCODE for type OP
            1  = OP_PING
            2  = OP_PONG

    route: uint32 (I)          # optional for REQUEST and REPLY
    buffer: bytes len(size)

TCPConn supports Bi-Directional Push->Pull and Request<->Response
"""

PACKET_PUSH = 0
PACKET_REQUEST = 1 << 30
PACKET_REPLY = 2 << 30
PACKET_TYPE_MASK = PACKET_REQUEST | PACKET_REPLY
PACKET_SIZE_MASK = ~PACKET_TYPE_MASK


class TCPConn(object):
    @classmethod
    def connect(klass, hub, host, port):
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.connect((host, port))
        conn.setblocking(0)
        return klass(hub, conn)

    def __init__(self, hub, conn):
        self.hub = hub
        self.conn = conn
        self.conn.setblocking(0)
        self.stopping = False
        self.closed = False

        # used to track calls, and incoming requests
        self.call_route = 0
        self.call_outstanding = {}

        self.pull = hub.channel()

        self.serve = hub.channel()
        self.serve_in_progress = 0
        ##

        self.recv_ready = hub.event(True)
        self.recv_buffer = ""
        self.recv_closed = False

        self.pong = hub.event(False)

        self.events = hub.register(
            conn.fileno(),
            select.EPOLLIN | select.EPOLLHUP | select.EPOLLERR)

        hub.spawn(self.event_loop)
        hub.spawn(self.recv_loop)

    def event_loop(self):
        while True:
            try:
                fd, event = self.events.recv()
                if event & select.EPOLLERR or event & select.EPOLLHUP:
                    self.close()
                    return
                if event & select.EPOLLIN:
                    if self.recv_closed:
                        if not self.serve_in_progress:
                            self.close()
                        return
                    self.recv_ready.set()
            except Closed:
                self.stop()

    def recv_loop(self):
        def recvn(n):
            if n == 0:
                return ""

            ret = ""
            while True:
                m = n - len(ret)
                if self.recv_buffer:
                    ret += self.recv_buffer[:m]
                    self.recv_buffer = self.recv_buffer[m:]

                if len(ret) >= n:
                    break

                try:
                    self.recv_buffer = self.conn.recv(max(m, 4096))
                except socket.error, e:
                    # resource unavailable, block until it is
                    if e.errno == 11:  # EAGAIN
                        self.recv_ready.clear().wait()
                        continue
                    raise

                if not self.recv_buffer:
                    raise socket.error("closing connection")

            return ret

        while True:
            try:
                typ_size, = struct.unpack('<I', recvn(4))

                # handle ping / pong
                if PACKET_TYPE_MASK & typ_size == PACKET_TYPE_MASK:
                    if typ_size & PACKET_SIZE_MASK == 1:
                        # ping received, send pong
                        self._send(struct.pack('<I', PACKET_TYPE_MASK | 2))
                    else:
                        # pong recieved
                        self.pong.set()
                        self.pong.clear()
                    continue

                if PACKET_TYPE_MASK & typ_size:
                    route, = struct.unpack('<I', recvn(4))

                data = recvn(typ_size & PACKET_SIZE_MASK)

                if typ_size & PACKET_REQUEST:
                    self.serve_in_progress += 1
                    self.serve.send((route, data))
                    continue

                if typ_size & PACKET_REPLY:
                    if route not in self.call_outstanding:
                        log.warning("Missing route: %s" % route)
                        continue
                    self.call_outstanding[route].send(data)
                    del self.call_outstanding[route]
                    if not self.call_outstanding and self.stopping:
                        self.close()
                        break
                    continue

                # push packet
                self.pull.send(data)
                continue

            except Exception, e:
                if type(e) != socket.error:
                    log.exception(e)
                self.recv_closed = True
                self.stop()
                break

    def push(self, data):
        self.send(0, PACKET_PUSH, data)

    def call(self, data):
        # TODO: handle wrap around
        self.call_route += 1
        self.call_outstanding[self.call_route] = self.hub.channel()
        self.send(self.call_route, PACKET_REQUEST, data)
        return self.call_outstanding[self.call_route]

    def reply(self, route, data):
        self.send(route, PACKET_REPLY, data)
        self.serve_in_progress -= 1
        if not self.serve_in_progress and self.stopping:
            self.close()

    def ping(self):
        self._send(struct.pack('<I', PACKET_TYPE_MASK | 1))

    def send(self, route, typ, data):
        assert len(data) < 2**30, "Data must be less than 1Gb"

        # TODO: is there away to avoid the duplication of data here?
        if PACKET_TYPE_MASK & typ:
            message = struct.pack('<II', typ | len(data), route) + data
        else:
            message = struct.pack('<I', typ | len(data)) + data

        self._send(message)

    def _send(self, message):
        try:
            self.conn.send(message)
        except Exception, e:
            if type(e) != socket.error:
                log.exception(e)
            self.close()
            raise

    def stop(self):
        if self.call_outstanding or self.serve_in_progress:
            self.stopping = True
            # if we aren't waiting for a reply, shutdown our read pipe
            if not self.call_outstanding:
                self.hub.unregister(self.conn.fileno())
                self.conn.shutdown(socket.SHUT_RD)
            return

        # nothing in progress, just close
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.hub.unregister(self.conn.fileno())
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except:
                pass
            self.conn.close()
            for ch in self.call_outstanding.values():
                ch.send(Exception("connection closed."))
            self.serve.close()


class TCPListener(object):
    def __init__(self, hub, host, port):
        self.hub = hub
        self.sock = s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(socket.SOMAXCONN)
        s.setblocking(0)

        self.port = s.getsockname()[1]
        self.accept = hub.channel()

        self.ch = hub.register(s.fileno(), select.EPOLLIN)
        hub.spawn(self.loop)

    def loop(self):
        while True:
            try:
                self.ch.recv()
                conn, host = self.sock.accept()
                conn = TCPConn(self.hub, conn)
                self.accept.send(conn)
            except Stop:
                self.stop()
                return

    def stop(self):
        self.hub.unregister(self.sock.fileno())
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        self.sock.close()


# HTTP #####################################################################


HTTP_VERSION = 'HTTP/1.1'


class Insensitive(object):
    Value = collections.namedtuple('Value', ['key', 'value'])

    def __init__(self):
        self.store = {}

    def __setitem__(self, key, value):
        self.store[key.lower()] = self.Value(key, value)

    def __getitem__(self, key):
        return self.store[key.lower()].value

    def __repr__(self):
        return repr(dict(self.store.itervalues()))


class HTTP(object):
    def __init__(self, hub):
        self.hub = hub

    def connect(self, url):
        return HTTPConn.connect(self.hub, url)


class HTTPConn(object):
    Status = collections.namedtuple('Status', ['version', 'code', 'message'])

    @classmethod
    def connect(klass, hub, url):
        parsed = urlparse.urlsplit(url)
        assert parsed.query == ''
        assert parsed.fragment == ''
        host, port = urllib.splitnport(parsed.netloc, 80)

        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.connect((host, port))

        self = klass(hub, conn)

        self.agent = 'vanilla/%s' % __version__

        self.default_headers = dict([
            ('Accept', '*/*'),
            ('User-Agent', self.agent),
            ('Host', parsed.netloc), ])
            # ('Connection', 'Close'), ])
        return self

    def __init__(self, hub, conn):
        self.hub = hub

        self.conn = conn
        self.conn.setblocking(0)
        self.ready = self.hub.register(
            self.conn.fileno(),
            select.EPOLLIN | select.EPOLLHUP | select.EPOLLERR)

        self.buff = ''
        self.responses = collections.deque()
        self.hub.spawn(self.receiver)

    def receiver(self):
        while True:
            status = self.read_status()

            ch = self.responses.popleft()
            ch.send(status)

            headers = self.read_headers()
            ch.send(headers)

            # TODO:
            # http://www.w3.org/Protocols/rfc2616/rfc2616-sec4.html#sec4.4
            body = self.read_length(int(headers['content-length']))
            ch.send(body)
            ch.close()

    def read_more(self):
        fd, event = self.ready.recv()
        if event & select.EPOLLERR or event & select.EPOLLHUP:
            raise Exception('EPOLLERR or EPOLLHUP')
        self.buff += self.conn.recv(4096)

    def read_line(self):
        while True:
            try:
                line, remainder = self.buff.split('\r\n', 1)
                self.buff = remainder
                return line
            except ValueError:
                self.read_more()

    def read_length(self, n):
        while True:
            if len(self.buff) >= n:
                ret = self.buff[:n]
                self.buff = self.buff[n:]
                return ret
            self.read_more()

    def read_status(self):
        version, code, message = self.read_line().split(' ', 2)
        code = int(code)
        return self.Status(version, code, message)

    def read_headers(self):
        headers = Insensitive()
        while True:
            line = self.read_line()
            if not line:
                break
            k, v = line.split(': ', 1)
            headers[k] = v
        return headers

    def request(self, method, path='/', headers=None, version=HTTP_VERSION):
        request_headers = {}
        request_headers.update(self.default_headers)
        if headers:
            request_headers.update(headers)

        request = '%s %s %s\r\n' % (method, path, version)
        headers = '\r\n'.join(
            '%s: %s' % (k, v) for k, v in request_headers.iteritems())

        self.conn.sendall(request+headers+'\r\n'+'\r\n')

        ch = self.hub.channel()
        self.responses.append(ch)
        return ch
