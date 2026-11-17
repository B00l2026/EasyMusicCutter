from pathlib import Path
import sys
import tempfile
import threading
import unittest

from musiccutter.diagnostics import configure_logging
from musiccutter.runtime import WorkspaceLock


class RuntimeTests(unittest.TestCase):
    def test_workspace_exclusion_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = WorkspaceLock(Path(folder)), WorkspaceLock(Path(folder))
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_separate_workspaces_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = WorkspaceLock(Path(folder) / 'one'), WorkspaceLock(Path(folder) / 'two')
            first.acquire()
            try:
                second.acquire()
                second.release()
            finally:
                first.release()

    def test_error_stack_is_persisted(self):
        original_sys, original_thread = sys.excepthook, threading.excepthook
        with tempfile.TemporaryDirectory() as folder:
            logger, recent, path = configure_logging(Path(folder))
            try:
                try:
                    raise RuntimeError('test failure detail')
                except RuntimeError:
                    logger.exception('audio job failed')
                text = path.read_text()
                self.assertIn('Traceback', text)
                self.assertIn('test failure detail', text)
                self.assertIn('audio job failed', '\n'.join(recent.entries))
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()
                sys.excepthook, threading.excepthook = original_sys, original_thread
