"""Run blocking work (disk scans, indexing, export plans) off the UI thread and get the result
back on it."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from traytrim.errors import describe_error  # noqa: F401  (re-exported for the GUI)

# Tasks stay referenced here until their worker is done, so they never depend on a parent
# widget being alive (closing the window mid-task must not delete what the worker reports to).
_live: set[BackgroundTask] = set()
_live_lock = threading.Lock()


class BackgroundTask(QObject):
    """fn(task) runs on a worker thread; signals arrive on the UI thread.

    fn can call task.report(...) as often as it likes (forwarded at most ~10 times a second,
    forced reports always get through) and should check task.cancelled regularly. A cancelled
    task sends `aborted` instead of a result or an error, unless keep_late_result is set: then
    work that ran to the end despite a late cancel still sends its result (an export that is
    already saved must not be reported as cancelled).
    """

    progress = Signal(tuple)
    finished = Signal(object)
    failed = Signal(object)
    aborted = Signal()
    _done = Signal()  # always last; releases the task on the UI thread after the others

    _MIN_INTERVAL = 0.1

    def __init__(self, fn: Callable[[BackgroundTask], Any], keep_late_result: bool = False):
        super().__init__()
        self._fn = fn
        self.keep_late_result = keep_late_result
        self._cancel = threading.Event()
        self._last_report = 0.0
        self.running = False

    @property
    def cancelled(self) -> threading.Event:
        return self._cancel

    def cancel(self) -> None:
        self._cancel.set()

    def report(self, *args: Any, force: bool = False) -> None:
        if self._cancel.is_set():
            return
        now = time.monotonic()
        if force or now - self._last_report >= self._MIN_INTERVAL:
            self._last_report = now
            _emit(self.progress, args)

    def start(self) -> None:
        self.running = True
        self._done.connect(self._release)
        with _live_lock:
            _live.add(self)
        QThreadPool.globalInstance().start(_Job(self))

    def _release(self) -> None:
        self.running = False
        with _live_lock:
            _live.discard(self)


def shutdown(timeout_ms: int = 15_000) -> bool:
    """Before the app exits: ask every running task to stop and wait for the worker threads,
    so none is still running Python or sending signals while Qt and Python shut down (that
    crashes the process on exit). True if they all finished in time."""
    with _live_lock:
        tasks = list(_live)
    for task in tasks:
        task.cancel()
    return QThreadPool.globalInstance().waitForDone(timeout_ms)


def _emit(signal, *value) -> None:
    try:
        signal.emit(*value)
    except RuntimeError:  # the app is shutting down and Qt already deleted the object
        pass


class _Job(QRunnable):
    def __init__(self, task: BackgroundTask):
        super().__init__()
        self._task = task

    def run(self) -> None:
        task = self._task
        try:
            result = task._fn(task)
        except Exception as e:  # handed to the failed signal on the UI thread
            if task.cancelled.is_set():
                _emit(task.aborted)
            else:
                _emit(task.failed, e)
        else:
            if task.cancelled.is_set() and not task.keep_late_result:
                _emit(task.aborted)
            else:
                _emit(task.finished, result)
        finally:
            _emit(task._done)


def run_in_background(fn: Callable[[], Any], on_done: Callable[[Any], None],
                      on_error: Callable[[Exception], None]) -> BackgroundTask:
    """Call fn() on a worker thread, then on_done(result) or on_error(exc) on the UI thread."""
    task = BackgroundTask(lambda _task: fn())
    task.finished.connect(on_done)
    task.failed.connect(on_error)
    task.start()
    return task
