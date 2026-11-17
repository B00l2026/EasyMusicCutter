#!/usr/bin/env python3
"""MusicCutter 4 entry point; v3 core imports and --batch --excel stay compatible."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading

from musiccutter import APP_VERSION
from musiccutter.diagnostics import configure_logging, data_directory
from musiccutter.engine import (batch_export, check_tools, decode_wave, export_one, get_tools,
                               probe_audio, process, read_duration, run_batch)
from musiccutter.excel import load_excel, save_excel
from musiccutter.files import scan_audio
from musiccutter.models import (Cancelled, Clip, DEFAULT_LENGTH, MIN_LENGTH, SUPPORTED,
                               norm_tenth, output_name, suggested_name, time_label, timestamp,
                               unique_name, validate_range)


def headless(args: argparse.Namespace) -> int:
    configure_logging(data_directory())
    tools = get_tools(args.ffmpeg or '')
    check_tools(tools)
    if args.excel:
        book = Path(args.excel).expanduser().resolve()
        clips = load_excel(book, Path(args.input).resolve() if args.input else None)
        folder = Path(args.output).resolve() if args.output else book.parent / 'output'
    else:
        inputs = [Path(path) for path in (args.files or [])]
        if args.folder:
            inputs.append(Path(args.folder))
        if not inputs:
            raise ValueError('请提供 --files、--folder 或 --excel')
        scanned = scan_audio(inputs, recursive=not args.no_recursive)
        if scanned.issues:
            for issue in scanned.issues:
                print('扫描问题：' + issue, file=sys.stderr)
        start, length = validate_range(args.start, args.duration)
        used: set[str] = set()
        clips = [Clip(path.name, path, start, length, unique_name(suggested_name(path, args.format), used))
                 for path in scanned.paths]
        folder = Path(args.output).resolve() if args.output else Path.cwd() / 'output'
    if not clips:
        print('没有可处理的音频或片段', file=sys.stderr)
        return 2
    cancel = threading.Event()
    import signal
    original = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        result = run_batch(clips, folder, tools, args.overwrite, cancel,
            lambda kind, value: print(f'{value[0]}/{value[1]} {value[3]} {value[4]}') if kind == 'export_row' else None)
    finally:
        signal.signal(signal.SIGINT, original)
    print(f'成功 {result.success}，跳过 {result.skipped}，失败 {result.failed}，'
          f'取消 {result.cancelled}，未开始 {result.pending}；报告：{result.report}')
    return 130 if result.was_cancelled else int(result.failed > 0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f'MusicCutter {APP_VERSION} · 本地音频片段剪辑')
    parser.add_argument('--batch', action='store_true', help='命令行模式')
    parser.add_argument('--excel', help='旧版 Excel 裁剪配置')
    parser.add_argument('--input', help='Excel 对应的源音频目录')
    parser.add_argument('--files', nargs='+', help='批量模式输入音频')
    parser.add_argument('--folder', help='批量模式扫描目录')
    parser.add_argument('--no-recursive', action='store_true', help='不扫描子文件夹')
    parser.add_argument('--start', default='0', help='开始时间，秒或分:秒')
    parser.add_argument('--duration', default='15', help='片段时长，秒')
    parser.add_argument('--format', choices=('mp3', 'flac', 'wav'), default='mp3', help='片段输出格式')
    parser.add_argument('--output', help='片段输出目录')
    parser.add_argument('--ffmpeg', help='FFmpeg 可执行文件路径')
    parser.add_argument('--overwrite', action='store_true', help='覆盖同名输出，仍禁止覆盖源音频')
    parser.add_argument('--self-check', action='store_true', help='检查音频引擎，不打开窗口')
    parser.add_argument('--version', action='version', version=APP_VERSION)
    args = parser.parse_args(argv)
    if args.self_check:
        try:
            tools = get_tools(args.ffmpeg or '')
            print(check_tools(tools))
            print('导出可用；试听：' + ('可用' if tools[2] else '不可用，缺少 FFplay'))
            return 0
        except Exception as exc:
            print('音频引擎检查失败：' + str(exc), file=sys.stderr)
            return 2
    if args.batch:
        try:
            return headless(args)
        except Exception as exc:
            print('错误：' + str(exc), file=sys.stderr)
            return 2
    import tkinter as tk
    from tkinter import messagebox
    from musiccutter.runtime import WorkspaceLock
    from musiccutter.ui import MusicCutterApp
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print('无法启动桌面界面；请使用带 Tkinter 的 Python 并在桌面会话运行。\n' + str(exc), file=sys.stderr)
        return 2
    workspace_lock = WorkspaceLock(data_directory())
    try:
        workspace_lock.acquire()
    except (OSError, RuntimeError) as exc:
        messagebox.showerror('无法打开工作区', str(exc), parent=root)
        root.destroy()
        return 2
    try:
        MusicCutterApp(root, ffmpeg=args.ffmpeg or '')
        root.mainloop()
    finally:
        workspace_lock.release()
    return 0


def __getattr__(name):
    if name == 'MusicCutterApp':
        from musiccutter.ui import MusicCutterApp
        return MusicCutterApp
    raise AttributeError(name)


if __name__ == '__main__':
    raise SystemExit(main())
