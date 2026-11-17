"""Run: xvfb-run -a python tests/test_gui_stress.py"""
import os, subprocess, sys, tempfile, time
from pathlib import Path
os.environ['SDL_AUDIODRIVER']='dummy'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tkinter as tk
from music_cutter_gui import MusicCutterApp, Clip

with tempfile.TemporaryDirectory() as temp:
    folder = Path(temp)
    src=folder/'test.mp3'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=440:duration=6',
                    '-c:a','libmp3lame',str(src)],check=True)
    tracks=[]
    for i in range(50):
        file=folder/f'测试歌曲_{i+1:03}.mp3'
        os.link(src,file)
        tracks.append(Clip(file.name,file,0,2.0,f'clip_{i+1:03}.mp3'))
    r=tk.Tk()
    a=MusicCutterApp(r)
    a.audio_root=folder
    start=time.monotonic()
    a._append_clips(tracks)
    r.update()
    print('import 50 UI',round(time.monotonic()-start,3))
    a.excel_path=folder/'test.xlsx'
    a._save_excel()
    wait=time.monotonic()
    while a.saving and time.monotonic()-wait<10:
        r.update();time.sleep(.02)
    assert not a.saving, 'save hung'
    print('saved',a.status_var.get())
    # Rapid selection and apply config to mirror real report.
    max_tick=0
    start=time.monotonic()
    for i,c in enumerate(tracks):
        a.tree.selection_set(str(c.uid))
        a._on_select(None)
        a.length_var.set('1.1')
        assert a._apply(),f'apply failed at {i}: {a.editor_var.get()}'
        t=time.monotonic();r.update();max_tick=max(max_tick,time.monotonic()-t)
    print('50 switches and saves',round(time.monotonic()-start,3),'max update',round(max_tick,3))
    assert a._current().uid==tracks[-1].uid
    # Wait for selected song to have duration; earlier wave requests should be dropped.
    start=time.monotonic()
    while a.track_duration<1 and time.monotonic()-start<5:
        r.update();time.sleep(.02)
    assert a.track_duration>1, 'selection metadata stalled'
    print('last duration',a.track_duration)
    a._start_preview()
    start=time.monotonic()
    observed=[]
    while time.monotonic()-start<8:
        r.update();time.sleep(.04)
        observed.append(a.preview_var.get())
        if '试听结束' in a.preview_var.get():
            break
    print('preview final',a.preview_var.get())
    assert any('FFplay 已启动' in s for s in observed), 'never started'
    assert any('试听结束' in s for s in observed), 'preview did not complete'
    a._show_wave()
    start=time.monotonic()
    while not a.wave_peaks and time.monotonic()-start < 10:
        r.update();time.sleep(.03)
    assert a.wave_peaks and len(a.wave_peaks)==640, 'wave missing'
    print('wave parsed',round(time.monotonic()-start,2))
    a._on_close()
print('GUI stress PASS')
