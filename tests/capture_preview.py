"""Capture the real application window using generated demo audio; no user media."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from musiccutter.engine import get_tools
from musiccutter.models import Clip
from musiccutter.ui import MusicCutterApp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--keep-open', action='store_true')
    parser.add_argument('--layout-only', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='musiccutter-preview-') as folder:
        base = Path(folder).resolve()
        tools = get_tools()
        source = base / '示例音频 · 钢琴练习.wav'
        subprocess.run([tools[0], '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'aevalsrc=0.18*sin(2*PI*440*t)*(0.55+0.45*sin(2*PI*0.8*t)):s=44100:d=90', str(source)], check=True)
        second = base / '示例音频 · 现场录音.flac'
        subprocess.run([tools[0], '-v', 'error', '-y', '-i', str(source), str(second)], check=True)
        root = tk.Tk()
        app = MusicCutterApp(root, data_dir=base / 'data', restore=False)
        app.output_dir = Path(__file__).resolve().parents[1] / 'output'
        app.output_var.set(str(app.output_dir))
        clips = [Clip(source.name, source, 12.3, 15.0, '钢琴练习_开场.mp3', state='待导出'),
                 Clip(source.name, source, 45.0, 20.0, '钢琴练习_第二段.flac', state='待导出'),
                 Clip(second.name, second, 5.0, 10.0, '现场录音_片段.mp3', state='待导出')]
        app._append_clips(clips)
        root.update()
        def wave():
            if not app.tools:
                root.after(100, wave)
            else:
                app._show_wave()
                root.after(500, capture)
        def capture():
            if not app.wave_peaks:
                root.after(100, capture)
                return
            root.update_idletasks()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            output = Path(__file__).resolve().parents[1] / '界面预览.png'
            print('Window:', root.winfo_geometry(), 'screen=', root.winfo_screenwidth(), root.winfo_screenheight(), flush=True)
            # Confirm all controls stay within the content rectangle at minimum height.
            for name in ('tree', 'start_entry', 'name_entry', 'listen_button', 'export_button', 'output_entry'):
                widget = getattr(app, name)
                print(name, widget.winfo_rootx() - x, widget.winfo_rooty() - y,
                      widget.winfo_width(), widget.winfo_height(), 'mapped=', widget.winfo_ismapped(), flush=True)
            if not args.layout_only:
                try:
                    if sys.platform == 'darwin':
                        subprocess.run(['screencapture', '-x', '-R', f'{x},{y},{root.winfo_width()},{root.winfo_height()}', str(output)], check=True)
                    else:
                        from PIL import ImageGrab
                        ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(output)
                    print('Screenshot:', output, flush=True)
                except Exception as exc:
                    print('Screenshot unavailable:', exc, flush=True)
            if not args.keep_open:
                app._on_close()
        root.after(200, wave)
        root.mainloop()


if __name__ == '__main__':
    main()
