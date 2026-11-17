from __future__ import annotations

from array import array
import csv
import logging
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from musiccutter.engine import export_one, get_tools, process, run_batch
from musiccutter.models import Cancelled, Clip


class EngineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.tools = get_tools()
        cls.source = cls.base / 'known.wav'
        cls.rate = 16000
        # A deterministic nonperiodic signal detects both wrong start and wrong length.
        rng = random.Random(4201)
        cls.samples = array('h', (rng.randrange(-6000, 6000) for _ in range(cls.rate * 62)))
        with wave.open(str(cls.source), 'wb') as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(cls.rate)
            file.writeframes(cls.samples.tobytes())
        logger = logging.getLogger('musiccutter')
        logger.addHandler(logging.NullHandler())
        logger.propagate = False

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=self.base))

    def batch(self, clips, folder=None, overwrite=False, cancel=None, progress=None):
        return run_batch(clips, folder or self.folder, self.tools, overwrite,
                         cancel or threading.Event(), progress or (lambda *_: None))

    def pcm(self, path):
        result = subprocess.run([self.tools[0], '-v', 'error', '-i', str(path), '-map', '0:a:0',
            '-f', 's16le', '-ac', '1', '-ar', str(self.rate), 'pipe:1'], capture_output=True, check=True)
        return result.stdout

    def test_lossless_sample_start_and_length(self):
        for extension in ('wav', 'flac'):
            for start, length in ((1.2, .1), (2.3, 2.7), (0, 55.5)):
                with self.subTest(extension=extension, start=start, length=length):
                    name = f'{extension}-{start}-{length}.{extension}'
                    result = self.batch([Clip(self.source.name, self.source, start, length, name)])
                    self.assertEqual((result.success, result.failed), (1, 0))
                    decoded = self.pcm(self.folder / name)
                    first = int(round(start * self.rate))
                    last = first + int(round(length * self.rate))
                    self.assertEqual(decoded, self.samples[first:last].tobytes())

    def test_mp3_decoded_precision(self):
        for start, length in ((1.2, .1), (2.3, 1.5), (4.1, 45.0)):
            with self.subTest(length=length):
                name = f'timing-{length}.mp3'
                result = self.batch([Clip(self.source.name, self.source, start, length, name)])
                self.assertEqual(result.success, 1)
                decoded = array('h')
                decoded.frombytes(self.pcm(self.folder / name))
                self.assertEqual(len(decoded), int(round(length * self.rate)))
                expected = self.samples[int(round(start * self.rate)):int(round((start + length) * self.rate))]
                dot = sum(a * b for a, b in zip(decoded, expected))
                norm = math.sqrt(sum(a * a for a in decoded) * sum(b * b for b in expected))
                self.assertGreater(dot / norm, .85, 'MP3 sample alignment drifted')

    def test_common_input_formats(self):
        codecs = {
            'mp3': ['-c:a', 'libmp3lame', '-q:a', '2'], 'flac': ['-c:a', 'flac'],
            'm4a': ['-c:a', 'aac'], 'aac': ['-c:a', 'aac'],
            'ogg': ['-ac', '2', '-c:a', 'vorbis', '-strict', '-2'],
            'opus': ['-ar', '48000', '-c:a', 'opus', '-strict', '-2'],
            'aiff': ['-c:a', 'pcm_s16be'], 'wma': ['-c:a', 'wmav2'],
        }
        for extension, codec in codecs.items():
            with self.subTest(extension=extension):
                source = self.folder / ('输入.' + extension)
                subprocess.run([self.tools[0], '-v', 'error', '-y', '-i', str(self.source),
                    '-t', '4', *codec, str(source)], check=True, capture_output=True)
                result = self.batch([Clip(source.name, source, .5, 1.3, extension + '.mp3')])
                self.assertEqual((result.success, result.failed), (1, 0))

    def test_seek_matches_decoded_input_timeline(self):
        codecs = {'mp3': ['-c:a', 'libmp3lame', '-q:a', '2'], 'flac': ['-c:a', 'flac'], 'm4a': ['-c:a', 'aac']}
        for extension, codec in codecs.items():
            with self.subTest(extension=extension):
                source = self.folder / f'seek-input.{extension}'
                subprocess.run([self.tools[0], '-v', 'error', '-y', '-i', str(self.source),
                    '-t', '5', *codec, str(source)], check=True, capture_output=True)
                decoded_source = array('h')
                decoded_source.frombytes(self.pcm(source))
                for start in (0.1, 1.2, 2.3, 3.9):
                    output = f'seek-{extension}-{start}.wav'
                    result = self.batch([Clip(source.name, source, start, .7, output)])
                    self.assertEqual(result.success, 1)
                    actual = array('h')
                    actual.frombytes(self.pcm(self.folder / output))
                    first = int(round(start * self.rate))
                    expected = decoded_source[first:first + 11200]
                    self.assertEqual(len(actual), len(expected))
                    dot = sum(a * b for a, b in zip(actual, expected))
                    norm = math.sqrt(sum(a * a for a in actual) * sum(b * b for b in expected))
                    self.assertGreater(dot / norm, .99, f'{extension} start time {start} drifted')

    def test_existing_output_and_publish_race(self):
        output = self.folder / 'existing.mp3'
        output.write_bytes(b'keep this')
        clip = Clip(self.source.name, self.source, 0, .1, output.name)
        result = self.batch([clip])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(output.read_bytes(), b'keep this')
        race = Clip(self.source.name, self.source, 0, .1, 'race.mp3')
        real_link = os.link
        def competing_writer(src, dest):
            Path(dest).write_bytes(b'another writer')
            return real_link(src, dest)
        with patch('musiccutter.engine.os.link', side_effect=competing_writer):
            result = self.batch([race])
        self.assertEqual(result.skipped, 1)
        self.assertEqual((self.folder / 'race.mp3').read_bytes(), b'another writer')
        self.assertEqual(list(self.folder.glob('.musiccutter_*')), [])

    def test_protects_every_source(self):
        first = self.folder / 'a.wav'
        second = self.folder / 'b.wav'
        first.write_bytes(self.source.read_bytes())
        second.write_bytes(self.source.read_bytes())
        before = second.read_bytes()
        clips = [Clip(first.name, first, 0, .1, second.name), Clip(second.name, second, 0, .1, 'safe.wav')]
        result = self.batch(clips, overwrite=True)
        self.assertEqual((result.success, result.failed), (1, 1))
        self.assertEqual(second.read_bytes(), before)

    def test_duplicate_names_both_fail_and_corrupt_isolated(self):
        broken = self.folder / 'broken.mp3'
        broken.write_text('not audio')
        clips = [Clip(self.source.name, self.source, 0, .1, 'same.mp3'),
                 Clip(self.source.name, self.source, .1, .1, 'SAME.mp3'),
                 Clip(broken.name, broken, 0, .1, 'broken-result.mp3'),
                 Clip(self.source.name, self.source, .2, .1, 'works.mp3')]
        result = self.batch(clips)
        self.assertEqual((result.success, result.failed), (1, 3))
        self.assertFalse((self.folder / 'same.mp3').exists())
        with result.report.open(encoding='utf-8-sig', newline='') as file:
            self.assertEqual(len(list(csv.DictReader(file))), 4)

    def test_cancel_during_encode_reports_every_task(self):
        cancel = threading.Event()
        clips = [Clip(self.source.name, self.source, 0, 50, f'{i}.mp3') for i in range(5)]
        def progress(kind, value):
            if kind == 'export_stage' and value[-1] == '编码':
                cancel.set()
        result = self.batch(clips, cancel=cancel, progress=progress)
        self.assertEqual((result.success, result.failed, result.cancelled, result.pending), (0, 0, 1, 4))
        self.assertTrue(result.was_cancelled)
        self.assertEqual(list(self.folder.glob('*.mp3')), [])
        self.assertEqual(list(self.folder.glob('.musiccutter_*')), [])
        with result.report.open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]['状态'], '已取消')
        self.assertEqual(rows[-1]['状态'], '未开始')

    def test_cancel_before_start_has_complete_report(self):
        cancel = threading.Event()
        cancel.set()
        result = self.batch([Clip(self.source.name, self.source, 0, .1, 'a.mp3')], cancel=cancel)
        self.assertEqual((result.pending, result.failed), (1, 0))
        self.assertTrue(result.report.is_file())

    def test_invalid_range_no_partial_output(self):
        for start, length in ((-1, 1), (float('nan'), .1), (61.9, .2), (0, 0)):
            result = self.batch([Clip(self.source.name, self.source, start, length, 'bad.mp3')])
            self.assertEqual(result.failed, 1)
        self.assertFalse((self.folder / 'bad.mp3').exists())

    def test_process_timeout_and_cancel_reap_child(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            process([sys.executable, '-c', 'import time;time.sleep(20)'], timeout=.15)
        self.assertLess(time.monotonic() - started, 2)
        cancel = threading.Event()
        timer = threading.Timer(.15, cancel.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                process([sys.executable, '-c', 'import time;time.sleep(20)'], timeout=20, stop=cancel.is_set)
        finally:
            timer.join()


if __name__ == '__main__':
    unittest.main()
