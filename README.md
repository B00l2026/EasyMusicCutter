# MusicCutter 1.0

本地批量音频剪辑。支持逐首设定、一首多个片段、0.1 秒精度、自动恢复、可取消导出与持久日志。

macOS 双击 `启动 MusicCutter.command`；Windows 双击 `启动 MusicCutter.bat`。

```bash
python3 music_cutter_gui.py
python3 music_cutter_gui.py --self-check
```

运行需要 Python 3.10+（含 Tkinter）与 FFmpeg/FFprobe，试听额外需要 FFplay。Excel 兼容功能才需要 `requirements.txt` 的 openpyxl。

[使用说明](使用说明.md)
