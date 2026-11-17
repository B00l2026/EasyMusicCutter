"""Prevent two desktop processes from overwriting the same automatic workspace."""
from __future__ import annotations

import os
from pathlib import Path


class WorkspaceLock:
    def __init__(self, folder: Path):
        self.path = folder / 'workspace.lock'
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError('另一个 MusicCutter 正在使用此工作区。请回到已打开的窗口，或关闭它后重试。') from exc
        self._file = stream
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()).encode('ascii'))
        stream.flush()

    def release(self) -> None:
        if self._file is None:
            return
        stream, self._file = self._file, None
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
