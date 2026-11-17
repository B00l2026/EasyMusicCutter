"""One serial worker per lane; replace obsolete media requests instead of queueing."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import queue
import threading
from typing import Any, Callable

from .models import Cancelled

LOG = logging.getLogger('musiccutter')


@dataclass(frozen=True)
class Event:
    lane: str
    token: int
    kind: str
    value: Any = None


@dataclass
class Context:
    lane: str
    token: int
    cancel: threading.Event
    events: queue.Queue

    def emit(self, kind: str, value: Any = None) -> None:
        self.events.put(Event(self.lane, self.token, kind, value))

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


class Worker:
    def __init__(self, name: str, events: queue.Queue):
        self.name, self.events = name, events
        self.token = 0
        self._condition = threading.Condition()
        self._active: Context | None = None
        self._pending: tuple[Context, Callable] | None = None
        self._closed = False
        self.thread = threading.Thread(target=self._run, name=f'mc-{name}', daemon=True)
        self.thread.start()

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._active is not None or self._pending is not None

    def submit(self, work: Callable[[Context], Any]) -> int:
        with self._condition:
            if self._closed:
                raise RuntimeError('工作线程已关闭')
            if self._active:
                self._active.cancel.set()
            if self._pending:
                self._pending[0].cancel.set()
            self.token += 1
            context = Context(self.name, self.token, threading.Event(), self.events)
            self._pending = context, work
            self._condition.notify_all()
            return self.token

    def cancel(self) -> None:
        with self._condition:
            if self._active:
                self._active.cancel.set()
            if self._pending:
                self._pending[0].cancel.set()
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            if self._active:
                self._active.cancel.set()
            if self._pending:
                self._pending[0].cancel.set()
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed and self._pending is None:
                    return
                context, work = self._pending
                self._pending = None
                self._active = context
            try:
                context.emit('started')
                value = work(context)
                context.emit('done', value)
            except Cancelled:
                context.emit('cancelled')
            except Exception as exc:
                LOG.exception('后台任务失败 lane=%s token=%s', self.name, context.token)
                context.emit('error', str(exc))
            finally:
                with self._condition:
                    self._active = None
                context.emit('idle')
