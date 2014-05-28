import collections
import traceback
import select
import signal
import heapq
import fcntl
import time
import sys
import os


from greenlet import getcurrent
from greenlet import greenlet


class Timeout(Exception):
    pass


class Closed(Exception):
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

        if self.pipeline:
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
        self.mapper = {}
        self.reverse_mapper = {}

    def register(self, sig):
        pipe_r, pipe_w = ospipe()

        def handler(sig, frame):
            os.write(pipe_w, chr(sig))
        signal.signal(sig, handler)

        @self.hub.register(pipe_r, select.EPOLLIN)
        def _((fd, event)):
            sig = ord(os.read(fd, 1))
            for ch in self.mapper[sig]:
                ch.send(sig)

    def subscribe(self, *signals):
        out = self.hub.channel()
        self.reverse_mapper[out] = signals

        for sig in signals:
            if sig not in self.mapper:
                self.register(sig)
                self.mapper[sig] = [out]
            else:
                self.mapper[sig].append(out)

        return out


class Hub(object):
    def __init__(self):
        self.ready = collections.deque()
        self.scheduled = Scheduler()
        self.stopped = self.event()

        self.epoll = select.epoll()
        self.registered = {}

        self.signal = Signal(self)

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
            del self.registered[fd]

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
