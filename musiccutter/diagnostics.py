"""Persistent rotating logs and application-level exception capture."""
from __future__ import annotations

from collections import deque
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
import threading

from . import APP_VERSION


class RecentHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.entries: deque[str] = deque(maxlen=500)

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append(self.format(record))


def data_directory() -> Path:
    override = os.environ.get('MUSICCUTTER_DATA_DIR')
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / 'userdata'


def configure_logging(data_dir: Path) -> tuple[logging.Logger, RecentHandler, Path]:
    logger = logging.getLogger('musiccutter')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    recent = RecentHandler()
    formatter = logging.Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s', '%Y-%m-%d %H:%M:%S')
    recent.setFormatter(formatter)
    logger.addHandler(recent)
    log_path = data_dir / 'logs' / 'musiccutter.log'
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        disk = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=4, encoding='utf-8')
        disk.setFormatter(formatter)
        logger.addHandler(disk)
    except OSError:
        logger.exception('无法写入日志目录 %s', log_path.parent)

    def uncaught(exc_type, exc, tb) -> None:
        logger.critical('未捕获异常', exc_info=(exc_type, exc, tb))
    sys.excepthook = uncaught
    threading.excepthook = lambda args: uncaught(args.exc_type, args.exc_value, args.exc_traceback)
    logger.info('启动 MusicCutter %s | %s | Python %s', APP_VERSION, platform.platform(), sys.version.split()[0])
    return logger, recent, log_path
