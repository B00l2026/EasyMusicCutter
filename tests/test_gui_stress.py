"""Opt-in real desktop tests: MUSICCUTTER_GUI_TESTS=1 python3 -m unittest discover -s tests -p test_gui_stress.py -v"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from musiccutter.engine import get_tools
from musiccutter.models import Clip
from musiccutter.storage import load_project


@unittest.skipUnless(os.environ.get('MUSICCUTTER_GUI_TESTS') == '1', 'requires a desktop session; opt in explicitly')
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name).resolve()
        cls.source = cls.base / '测试音频.wav'
        tools = get_tools()
        subprocess.run([tools[0], '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=62', str(cls.source)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        import tkinter as tk
        from musiccutter.ui import MusicCutterApp
        self.data = Path(tempfile.mkdtemp(dir=self.base))
        self.root = tk.Tk()
        self.app = MusicCutterApp(self.root, data_dir=self.data, restore=False)
        self.pump(lambda: self.app.tools is not None)

    def pump(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertTrue(condition(), f'timed out: {self.app.status_var.get()} / {self.app.busy_var.get()}')
        if not self.app._destroyed:
            self.root.update()

    def tearDown(self):
        if not self.app._destroyed:
            with patch('musiccutter.ui.messagebox.askyesno', return_value=True):
                self.app._on_close()
            self.pump(lambda: self.app._destroyed)
        for worker in self.app.workers.values():
            worker.thread.join(2)
            self.assertFalse(worker.thread.is_alive(), f'{worker.name} did not shut down')

    def append(self, count=3):
        clips = [Clip(self.source.name, self.source, i / 10, 1.5, f'片段_{i}.mp3') for i in range(count)]
        self.app._append_clips(clips)
        self.pump(lambda: not self.app._insert_pending and self.app.track_duration > 0)
        return clips

    def test_edit_switch_multiple_segments_and_autosave(self):
        clips = self.append()
        self.app.start_var.set('01:00.1')
        self.app.length_var.set('1.2')
        self.app._select(clips[1].uid)
        self.assertEqual((clips[0].start, clips[0].length), (60.1, 1.2))
        self.app._clone()
        self.assertEqual(len(self.app.clips), 4)
        self.assertEqual(len({c.output for c in self.app.clips}), 4)
        self.pump(lambda: self.app.persisted_revision == self.app.revision)
        restored = load_project(self.app.recovery_path)
        self.assertEqual(len(restored.clips), 4)
        self.assertEqual(restored.clips[0].start, 60.1)

    def test_invalid_draft_survives_switch_close_and_recovery(self):
        clips = self.append()
        self.app.length_var.set('错误输入')
        self.app._select(clips[1].uid)
        self.assertIn(clips[0].uid, self.app.drafts)
        self.app._export()
        self.assertFalse(self.app.exporting)
        self.assertEqual(self.app.selected, clips[0].uid)
        self.assertEqual(self.app.length_var.get(), '错误输入')
        self.app._on_close()
        self.pump(lambda: self.app._destroyed)
        import tkinter as tk
        from musiccutter.ui import MusicCutterApp
        self.root = tk.Tk()
        self.app = MusicCutterApp(self.root, data_dir=self.data, restore=True)
        self.pump(lambda: self.app.recovery_ready and not self.app.io_busy and self.app.tools is not None)
        self.assertEqual(self.app.length_var.get(), '错误输入')
        self.assertEqual(len(self.app.drafts), 1)

    def test_thousand_files_fifty_switches(self):
        clips = []
        for i in range(1000):
            source = self.data / f'歌曲_{i:04}.wav'
            os.link(self.source, source)
            clips.append(Clip(source.name, source, 0, .5, f'{i:04}.mp3'))
        started = time.monotonic()
        self.app._append_clips(clips)
        self.pump(lambda: not self.app._insert_pending)
        import_seconds = time.monotonic() - started
        max_update = 0.0
        for clip in clips[:50]:
            self.app._select(clip.uid)
            self.app.length_var.set('.8')
            started = time.monotonic()
            self.root.update()
            max_update = max(max_update, time.monotonic() - started)
        self.assertEqual(len(self.app.clips), 1000)
        self.assertEqual(len(self.app.groups), 1000)
        self.assertLess(max_update, .5)
        print(f'\nPERF 1000 imports={import_seconds:.3f}s, worst update after switch={max_update:.3f}s', flush=True)

    def test_export_locks_edits_and_cancel_reports(self):
        clips = self.append(10)
        self.app.output_dir = self.data / 'exports'
        self.app._export()
        self.assertTrue(self.app.exporting)
        self.assertEqual(str(self.app.start_entry['state']), 'disabled')
        before = len(self.app.clips)
        self.app._clone()
        self.app._delete()
        self.assertEqual(len(self.app.clips), before)
        self.app._cancel_export()
        self.pump(lambda: not self.app.exporting)
        self.assertTrue(self.app.last_report.is_file())
        self.assertEqual(self.app.last_result.failed, 0)
        self.assertEqual(self.app.last_result.pending + self.app.last_result.cancelled + self.app.last_result.success, 10)
        self.app._show_logs()
        self.root.update()
        self.assertTrue(self.app._log_window.winfo_exists())
        self.assertTrue(self.app.log_path.is_file())

    def test_waveform_and_preview(self):
        self.append(1)
        self.app._show_wave()
        self.pump(lambda: len(self.app.wave_peaks) == 640)
        os.environ['SDL_AUDIODRIVER'] = 'dummy'
        self.app._start_preview()
        self.pump(lambda: '试听结束' in self.app.preview_var.get())
        self.assertFalse(self.app.workers['preview'].busy)

    def test_edits_during_save_keep_latest_revision(self):
        self.append(2)
        import musiccutter.ui as ui
        original = ui.save_project
        entered = __import__('threading').Event()
        def slow_save(path, project):
            entered.set()
            time.sleep(.12)
            original(path, project)
        with patch('musiccutter.ui.save_project', side_effect=slow_save):
            self.app.length_var.set('1.1')
            self.app._apply()
            self.app._autosave()
            self.pump(entered.is_set)
            self.app.length_var.set('2.2')
            self.app._apply()
            self.pump(lambda: self.app.persisted_revision == self.app.revision)
        restored = load_project(self.app.recovery_path)
        self.assertEqual(restored.clips[0].length, 2.2)

    def test_minimum_window_keeps_every_action_visible(self):
        self.append(2)
        self.root.geometry('1050x740')
        self.root.update()
        self.app.status_var.set('导出完成：成功 10，跳过 2，失败 1，取消 0，未开始 0。点击「查看结果」查看完整的逐项处理报告。')
        self.root.update()
        top = self.root.winfo_rooty()
        bottom = top + self.root.winfo_height()
        for name in ('tree', 'start_entry', 'name_entry', 'listen_button', 'stop_button',
                     'preview_label', 'export_button', 'output_entry', 'status_label'):
            widget = getattr(self.app, name)
            with self.subTest(control=name):
                self.assertTrue(widget.winfo_ismapped(), name)
                if name != 'tree':  # the scrolling list intentionally gives up space
                    self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight() - 1, name)
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom, name)

    def test_remove_and_undo_preserves_range_and_draft(self):
        clips = self.append(2)
        self.app.length_var.set('unfinished')
        self.app._delete()
        self.assertEqual(len(self.app.clips), 1)
        self.app._undo_remove()
        self.assertEqual(len(self.app.clips), 2)
        self.assertEqual(self.app.selected, clips[0].uid)
        self.assertEqual(self.app.length_var.get(), 'unfinished')


if __name__ == '__main__':
    unittest.main()
