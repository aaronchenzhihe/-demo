"""线程/同步工具：Thread、Condition、Lock、EventQueue、Queue 等。

复制自 AI_chatbot 项目，完整支持 _thread 多线程和条件同步。
"""
import utime
import sys
import _thread
import osTimer
from usr.logging import getLogger

logger = getLogger("threading")


class Lock(object):

    def __init__(self):
        self.__lock = _thread.allocate_lock()
        self.__owner = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args, **kwargs):
        self.release()

    def acquire(self):
        flag = self.__lock.acquire()
        self.__owner = _thread.get_ident()
        return flag

    def release(self):
        self.__owner = None
        return self.__lock.release()

    def locked(self):
        return self.__lock.locked()

    @property
    def owner(self):
        return self.__owner


class _Waiter(object):
    """WARNING: Waiter object can only be used once."""

    def __init__(self):
        self.__lock = Lock()
        self.__lock.acquire()
        self.__gotit = True

    @property
    def __unlock_timer(self):
        timer = getattr(self, "__unlock_timer__", None)
        if timer is None:
            timer = osTimer()
            setattr(self, "__unlock_timer__", timer)
        return timer

    @property
    def __timer_lock(self):
        lock = getattr(self, "__timer_lock__", None)
        if lock is None:
            lock = Lock()
            setattr(self, "__timer_lock__", lock)
        return lock

    def __auto_release(self, _):
        with self.__timer_lock:
            self.__gotit = not self.__release()

    def acquire(self, timeout=None):
        if timeout is not None and timeout <= 0:
            raise ValueError("\"timeout\" must be a positive number.")
        gotit = self.__gotit
        if timeout:
            with self.__timer_lock:
                self.__unlock_timer.start(timeout * 1000, 0, self.__auto_release)
        self.__lock.acquire()  # block here
        if timeout:
            with self.__timer_lock:
                gotit = self.__gotit
            self.__unlock_timer.stop()
        return gotit

    def __release(self):
        try:
            self.__lock.release()
        except RuntimeError:
            return False
        return True

    def release(self):
        return self.__release()


class Condition(object):

    def __init__(self, lock=None):
        if lock is None:
            lock = Lock()
        self.__lock = lock
        self.__waiters = []
        self.acquire = self.__lock.acquire
        self.release = self.__lock.release

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args, **kwargs):
        self.release()

    def __is_owned(self):
        return self.__lock.locked() and self.__lock.owner == _thread.get_ident()

    def wait(self, timeout=None):
        if not self.__is_owned():
            raise RuntimeError("cannot wait on un-acquired lock.")
        waiter = _Waiter()
        self.__waiters.append(waiter)
        self.release()
        gotit = False
        try:
            gotit = waiter.acquire(timeout)
            return gotit
        finally:
            self.acquire()
            if not gotit:
                try:
                    self.__waiters.remove(waiter)
                except ValueError:
                    pass

    def wait_for(self, predicate, timeout=None):
        endtime = None
        remaining = timeout
        result = predicate()
        while not result:
            if remaining is not None:
                if endtime is None:
                    endtime = utime.time() + remaining
                else:
                    remaining = endtime - utime.time()
                    if remaining <= 0.0:
                        break
            self.wait(remaining)
            result = predicate()
        return result

    def notify(self, n=1):
        if not self.__is_owned():
            raise RuntimeError("cannot wait on un-acquired lock.")
        if n < 0:
            raise ValueError("invalid param, n should be >= 0.")
        waiters_to_notify = self.__waiters[:n]
        for waiter in waiters_to_notify:
            waiter.release()
            try:
                self.__waiters.remove(waiter)
            except ValueError:
                pass

    def notify_all(self):
        self.notify(n=len(self.__waiters))


class Event(object):

    def __init__(self):
        self.__flag = False
        self.__cond = Condition()

    def wait(self, timeout=None, clear=False):
        with self.__cond:
            result = self.__cond.wait_for(lambda: self.__flag, timeout=timeout)
            if result and clear:
                self.__flag = False
            return result

    def set(self):
        with self.__cond:
            self.__flag = True
            self.__cond.notify_all()

    def clear(self):
        with self.__cond:
            self.__flag = False

    def is_set(self):
        with self.__cond:
            return self.__flag


class EventSet(object):

    def __init__(self):
        self.__set = 0
        self.__cond = Condition()

    def wait(self, event_set, timeout=None, clear=False):
        with self.__cond:
            result = self.__cond.wait_for(
                lambda: (event_set & self.__set) == event_set, timeout=timeout)
            if result and clear:
                self.__set &= ~event_set
            return result

    def wait_any(self, event_set, timeout=None, clear=False):
        with self.__cond:
            result = self.__cond.wait_for(
                lambda: event_set & self.__set, timeout=timeout)
            if result and clear:
                self.__set &= ~event_set
            return result

    def set(self, event_set):
        with self.__cond:
            self.__set |= event_set
            self.__cond.notify_all()

    def clear(self, event_set):
        with self.__cond:
            self.__set &= ~event_set

    def is_set(self, event_set):
        with self.__cond:
            return (self.__set & event_set) == event_set

    def is_set_any(self, event_set):
        with self.__cond:
            return self.__set & event_set


class Semaphore(object):

    def __init__(self, value=0):
        self.__value = value
        self.__cond = Condition()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args, **kwargs):
        self.release()

    def counts(self):
        with self.__cond:
            return self.__value

    def acquire(self, block=True, timeout=None):
        with self.__cond:
            if not block:
                if self.__value > 0:
                    self.__value -= 1
                    return True
                else:
                    return False
            elif timeout is not None and timeout <= 0:
                raise ValueError("\"timeout\" must be a positive number.")
            else:
                if self.__cond.wait_for(lambda: self.__value > 0, timeout=timeout):
                    self.__value -= 1
                    return True
                else:
                    return False

    def release(self, n=1):
        if n < 1:
            raise ValueError("n must be one or more")
        with self.__cond:
            self.__value += n
            self.__cond.notify(n)

    def clear(self):
        with self.__cond:
            self.__value = 0


class BoundedSemaphore(Semaphore):

    def __init__(self, value=1):
        super().__init__(value)
        self.__initial_value = value

    def release(self, n=1):
        if n < 1:
            raise ValueError("n must be one or more")
        with self.__cond:
            if self.__value + n > self.__initial_value:
                raise ValueError("Semaphore released too many times")
            self.__value += n
            self.__cond.notify(n)


class Queue(object):

    class Full(Exception):
        pass

    class Empty(Exception):
        pass

    def __init__(self, max_size=100):
        self.queue = []
        self.__max_size = max_size
        self.__lock = Lock()
        self.__not_empty = Condition(self.__lock)
        self.__not_full = Condition(self.__lock)

    def _put(self, item):
        self.queue.append(item)

    def put(self, item, block=True, timeout=None):
        with self.__not_full:
            if not block:
                if len(self.queue) >= self.__max_size:
                    raise self.Full
            elif timeout is not None and timeout <= 0:
                raise ValueError("\"timeout\" must be a positive number.")
            else:
                if not self.__not_full.wait_for(
                        lambda: len(self.queue) < self.__max_size,
                        timeout=timeout):
                    raise self.Full
            self._put(item)
            self.__not_empty.notify()

    def _get(self):
        return self.queue.pop(0)

    def get(self, block=True, timeout=None):
        with self.__not_empty:
            if not block:
                if len(self.queue) == 0:
                    raise self.Empty
            elif timeout is not None and timeout <= 0:
                raise ValueError("\"timeout\" must be a positive number.")
            else:
                if not self.__not_empty.wait_for(
                        lambda: len(self.queue) != 0, timeout=timeout):
                    raise self.Empty
            item = self._get()
            self.__not_full.notify()
            return item

    def size(self):
        with self.__lock:
            return len(self.queue)

    def clear(self):
        with self.__lock:
            self.queue.clear()


class LifoQueue(Queue):

    def _put(self, item):
        self.queue.append(item)

    def _get(self):
        return self.queue.pop()


class Thread(object):
    DEFAULT_STACK_SIZE = _thread.stack_size()

    def __init__(self, target=None, args=(), kwargs=None):
        self.__target = target
        self.__args = args
        self.__kwargs = kwargs or {}
        self.__ident = None
        self.__stopped_event = Event()

    def is_running(self):
        if self.__ident is None:
            return False
        else:
            return _thread.threadIsRunning(self.__ident)

    def join(self, timeout=None):
        return self.__stopped_event.wait(timeout=timeout)

    def terminate(self):
        if self.is_running():
            _thread.stop_thread(self.ident)
            self.__ident = None
        self.__stopped_event.set()

    def start(self, stack_size=None):
        if self.__ident is not None:
            raise RuntimeError("threads can only be started once")
        if stack_size is not None:
            _thread.stack_size(stack_size * 1024)
        self.__ident = _thread.start_new_thread(self.__bootstrap, ())
        if stack_size is not None:
            _thread.stack_size(self.DEFAULT_STACK_SIZE)

    def __bootstrap(self):
        try:
            self.run()
        except Exception as e:
            sys.print_exception(e)
        finally:
            self.__stopped_event.set()

    def run(self):
        if self.__target:
            self.__target(*self.__args, **self.__kwargs)

    @property
    def ident(self):
        return self.__ident


class EventQueue(object):
    """一个简单的 FIFO 事件队列。"""

    def __init__(self, name="eq"):
        self._q = []
        self.name = name

    def put(self, event):
        try:
            self._q.append(event)
        except Exception:
            pass

    def get_all(self):
        try:
            evts = list(self._q)
            self._q.clear()
            return evts
        except Exception:
            return []