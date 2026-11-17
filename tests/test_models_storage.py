from __future__ import annotations

import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from musiccutter.files import scan_audio
from musiccutter.models import (Clip, name_key, norm_tenth, output_name, suggested_name,
                               timestamp, unique_name, validate_range)
from musiccutter.storage import Project, load_project, save_project
from musiccutter.workers import Worker


class TimingTests(unittest.TestCase):
    def test_decimal_rounding(self):
        for value, expected in [(0.05, .1), (.15, .2), (1.25, 1.3), ('80.35', 80.4)]:
            with self.subTest(value=value):
                self.assertEqual(norm_tenth(value), expected)

    def test_time_formats(self):
        self.assertEqual(timestamp('01:20.36'), 80.4)
        self.assertEqual(timestamp('1:02:03.4'), 3723.4)
        self.assertEqual(timestamp('01：20.5'), 80.5)
        self.assertEqual(validate_range('60', '75.3', 200), (60, 75.3))

    def test_reject_invalid_times(self):
        for value in ('1:60', '1:60:00', '-0.01', '-1:01', 'NaN', 'Infinity', '', None, True, '1:2:3:4'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                timestamp(value)
        for args in ((0, 0), (0, .04), (1, 2, 2.9), (float('nan'), 1), (0, 1, float('inf'))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_range(*args)

    def test_names_and_collisions(self):
        for name in ('../a.mp3', 'CON.mp3', 'a\x00.mp3', 'a.txt', 'x?.flac', '.mp3.', 'a' * 241 + '.mp3'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                output_name(name)
        self.assertEqual(output_name('正常片段.FLAC'), '正常片段.FLAC')
        used = {name_key('SONG.mp3')}
        self.assertEqual(unique_name('song.mp3', used), 'song_2.mp3')
        self.assertEqual(name_key('café.mp3'), name_key('cafe\u0301.mp3'))
        self.assertTrue(suggested_name(Path('x' * 200 + '.flac')).endswith('.mp3'))


class FilesAndStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_recursive_scan_and_duplicate(self):
        (self.base / 'nested').mkdir()
        first = self.base / 'A.MP3'
        first.touch()
        (self.base / 'nested' / 'B.flac').touch()
        (self.base / 'readme.txt').touch()
        (self.base / 'loop').symlink_to(self.base, target_is_directory=True)
        (self.base / 'alias.mp3').symlink_to(first)
        result = scan_audio([self.base, first])
        self.assertEqual(len(result.paths), 2)
        self.assertEqual(result.duplicates, 2)
        self.assertEqual(len(scan_audio([self.base], recursive=False).paths), 1)
        self.assertEqual(len(scan_audio([self.base / 'missing']).issues), 1)

    def test_cancel_scan_returns_partial(self):
        for i in range(180):
            (self.base / f'{i:03}.mp3').touch()
        cancel = threading.Event()
        result = scan_audio([self.base], stop=cancel.is_set, progress=lambda *_: cancel.set())
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.paths), 100)

    def test_hidden_folder_audio_is_included(self):
        hidden = self.base / '.recordings'
        hidden.mkdir()
        (hidden / 'track.flac').touch()
        self.assertEqual(len(scan_audio([self.base]).paths), 1)

    def test_project_roundtrip_with_draft(self):
        clip = Clip('missing.flac', self.base / 'missing.flac', 12.3, 80.2, '片段.flac', auto_length=True)
        draft = {'start': '错的时间', 'length': '0.', 'output': '未完成文件名'}
        path = self.base / 'project.mcut.json'
        save_project(path, Project([clip], self.base / 'out', {clip.uid: draft}, '/ffmpeg', False))
        loaded = load_project(path)
        restored = loaded.clips[0]
        self.assertEqual((restored.source, restored.start, restored.length, restored.output),
                         (clip.source.resolve(), clip.start, clip.length, clip.output))
        self.assertEqual(loaded.drafts[restored.uid], draft)
        self.assertTrue(restored.auto_length)
        self.assertFalse(loaded.recursive)

    def test_bad_project_does_not_partially_load(self):
        path = self.base / 'bad.json'
        for data in ({'schema': 99, 'clips': []}, {'schema': 1, 'clips': [{}]},
                     {'schema': True, 'clips': []}, {'schema': 1, 'clips': 'wrong'}):
            path.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_project(path)

    def test_atomic_save_preserves_old_file(self):
        path = self.base / 'saved.json'
        path.write_text('original')
        with patch('musiccutter.storage.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                save_project(path, Project())
        self.assertEqual(path.read_text(), 'original')
        self.assertEqual(list(self.base.glob('.musiccutter_*')), [])


class WorkerTests(unittest.TestCase):
    def test_replaces_obsolete_request(self):
        events = queue.Queue()
        worker = Worker('test', events)
        entered = threading.Event()
        completed = []
        def slow(ctx):
            entered.set()
            while not ctx.cancel.wait(.01):
                pass
            ctx.check()
        worker.submit(slow)
        self.assertTrue(entered.wait(2))
        latest = worker.submit(lambda ctx: completed.append('latest'))
        deadline = time.monotonic() + 2
        while worker.busy and time.monotonic() < deadline:
            time.sleep(.005)
        worker.close()
        worker.thread.join(2)
        self.assertEqual(completed, ['latest'])
        delivered = []
        while not events.empty():
            delivered.append(events.get())
        self.assertTrue(any(e.kind == 'cancelled' for e in delivered))
        self.assertTrue(any(e.token == latest and e.kind == 'done' for e in delivered))
