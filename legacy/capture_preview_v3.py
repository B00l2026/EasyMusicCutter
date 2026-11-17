import os, sys, time, subprocess, tempfile
from pathlib import Path
os.environ['SDL_AUDIODRIVER']='dummy'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tkinter as tk
from PIL import ImageGrab
from music_cutter_gui import MusicCutterApp, Clip
folder=Path(tempfile.mkdtemp(prefix='music-cutter-preview-'))
src=folder/'sample.mp3'
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=350:duration=30','-c:a','libmp3lame',str(src)],check=True)
r=tk.Tk()
a=MusicCutterApp(r)
a.audio_root=folder
clips=[]
for i in range(1,13):
    f=folder/f'示例歌曲_{i:02d}.mp3'
    os.link(src, f)
    clips.append(Clip(f.name,f,3.0,12.0,f'{i:03d}.mp3'))
a._append_clips(clips)
a._show_wave()
t=time.monotonic()
while not a.wave_peaks and time.monotonic()-t<6:
    r.update();time.sleep(.04)
a.status_var.set('已导入 12 首示例歌曲。选中歌曲后只加载时长；需要波形时再点击按钮。')
a.root.update()
import time as _t
_t.sleep(.25)
r.update()
img=ImageGrab.grab(bbox=(0,0,1190,800))
out=Path(__file__).resolve().parents[1] / '界面预览.png'
img.save(out)
print(out, img.size, img.getbbox())
a._on_close()
