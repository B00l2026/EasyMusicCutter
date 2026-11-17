"""Cancellable discovery without decoding files or following directory symlinks."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Callable, Iterable

from .models import Cancelled, SUPPORTED


@dataclass
class ScanResult:
    paths: list[Path] = field(default_factory=list)
    ignored: int = 0
    duplicates: int = 0
    issues: list[str] = field(default_factory=list)
    cancelled: bool = False


def scan_audio(inputs: Iterable[Path], recursive: bool = True,
               stop: Callable[[], bool] = lambda: False,
               progress: Callable[[int, int], None] = lambda found, seen: None) -> ScanResult:
    result = ScanResult()
    seen: set[str] = set()
    inspected = 0

    def add(path: Path) -> None:
        nonlocal inspected
        if stop():
            raise Cancelled()
        inspected += 1
        if path.suffix.casefold() not in SUPPORTED:
            result.ignored += 1
        else:
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_file():
                    result.ignored += 1
                    return
                key = os.path.normcase(str(resolved))
                if key in seen:
                    result.duplicates += 1
                else:
                    seen.add(key)
                    result.paths.append(resolved)
            except OSError as exc:
                result.issues.append(f'{path}：{exc}')
        if inspected % 100 == 0:
            progress(len(result.paths), inspected)

    try:
        for item in inputs:
            if stop():
                raise Cancelled()
            path = Path(item).expanduser()
            if path.is_dir():
                def onerror(exc: OSError) -> None:
                    result.issues.append(str(exc))
                for current, dirs, files in os.walk(path, followlinks=False, onerror=onerror):
                    if stop():
                        raise Cancelled()
                    dirs[:] = sorted(d for d in dirs if not Path(current, d).is_symlink())
                    for name in sorted(files):
                        add(Path(current, name))
                    if not recursive:
                        break
            elif path.is_file():
                add(path)
            else:
                result.issues.append(f'路径不存在或无法访问：{path}')
    except Cancelled:
        result.cancelled = True
    progress(len(result.paths), inspected)
    return result
