import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from music_cutter_gui import (Clip, batch_export, decode_wave, get_tools, load_excel,
                              read_duration, save_excel, timestamp, time_label, output_name,
                              Cancelled)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.src = self.base / 'music.mp3'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi',
            '-i','sine=frequency=440:duration=8','-c:a','libmp3lame', '-b:a','96k',str(self.src)],check=True)
        self.tools = get_tools()

    def tearDown(self):
        self.tmp.cleanup()

    def test_timing(self):
        self.assertEqual(timestamp('01:20.36'), 80.4)
        self.assertEqual(timestamp(7.94), 7.9)
        self.assertEqual(time_label(80.4), '01:20.4')
        with self.assertRaises(ValueError):
            output_name('../unsafe.mp3')

    def test_wave_and_export(self):
        d = read_duration(self.tools[1], self.src)
        self.assertGreater(d,7.9)
        wave = decode_wave(self.tools[0], self.src, lambda: False)
        self.assertEqual(len(wave),640)
        self.assertTrue(max(wave) > .1)
        clip = Clip(self.src.name,self.src,1.2,2.5,'001.mp3')
        messages=[]
        a,b,c,report,canceled = batch_export([clip],self.base/'out',self.tools,False,threading.Event(),lambda k,x: messages.append((k,x)))
        self.assertEqual((a,b,c,canceled),(1,0,0,False))
        self.assertTrue(report.is_file())
        self.assertAlmostEqual(read_duration(self.tools[1],self.base/'out'/'001.mp3'),2.5,delta=.35)
        a,b,c,_,_ = batch_export([clip],self.base/'out',self.tools,False,threading.Event(),lambda k,x: None)
        self.assertEqual((a,b,c),(0,1,0))

    def test_excel_and_50_rows(self):
        clips=[Clip(self.src.name,self.src,float(i%3),.8,f'{i:03}.mp3') for i in range(50)]
        excel=self.base/'songs.xlsx'
        save_excel(excel,clips,self.base)
        loaded=load_excel(excel,self.base)
        self.assertEqual(len(loaded),50)
        self.assertEqual(loaded[0].length,.8)
        done = batch_export(loaded,self.base/'out50',self.tools,False,threading.Event(),lambda k,x: None)
        self.assertEqual(done[:3],(50,0,0))

    def test_cancellation(self):
        cancel=threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            read_duration(self.tools[1],self.src,cancel.is_set)

if __name__ == '__main__':
    unittest.main()
