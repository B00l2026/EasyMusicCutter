#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MusicCutter v3.0 — fast, cross-platform, responsive desktop clip editor.

Requirements: Python 3.10+, Tkinter, openpyxl, ffmpeg and ffprobe (ffplay for preview).
No local server, remote API, PyAudio, matplotlib, or numpy required.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
import csv
from dataclasses import dataclass, field
from datetime import datetime, time as DateTime, timedelta
import itertools
import math
import os
from pathlib import Path
import queue
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Any

try:
    from openpyxl import Workbook, load_workbook
except ImportError as e:
    raise SystemExit('缺少 openpyxl。请运行：python -m pip install openpyxl') from e

APP_VERSION = '3.0'
SUPPORTED = {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.opus'}
MIN_LENGTH, MAX_LENGTH, DEFAULT_LENGTH = .1, 40.0, 15.0
WAVE_WIDTH, WAVE_HEIGHT, MAX_WAVE_CACHE = 640, 64, 5
HEADERS = ['原始文件名', '开始时间', '截取秒数', '输出文件名']
ALIASES = [
    {'原始文件名', '音频文件名', '文件名', 'filename', 'source', 'file'},
    {'开始时间', '截取开始时间', '开始位置', 'start'},
    {'截取秒数', '截取时长', '剪辑时长', '时长', 'duration', 'length'},
    {'输出文件名', '片段文件名', '保存文件名', 'output'},
]
SERIAL = itertools.count(1)

BG, PAPER, INK, GRAY, ACCENT, SUCCESS, ERROR = '#f4f6fb', '#ffffff', '#172a47', '#65738a', '#3365cf', '#087b62', '#ba324b'


class Cancelled(Exception):
    pass


def norm_tenth(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError('时间必须为有限数字')
    return round(value * 10 + 1e-8) / 10


def timestamp(value: Any) -> float:
    """Accept seconds, MM:SS.s, HH:MM:SS.s and Excel time values."""
    if value is None or isinstance(value, bool):
        raise ValueError('开始时间不能为空')
    if isinstance(value, timedelta):
        sec = value.total_seconds()
    elif isinstance(value, (DateTime, datetime)):
        t = value.time() if isinstance(value, datetime) else value
        sec = t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
    elif isinstance(value, (int, float)):
        sec = float(value)
    else:
        bits = str(value).strip().replace('：', ':').split(':')
        try:
            if len(bits) == 1:
                sec = float(bits[0])
            elif len(bits) == 2:
                sec = int(bits[0]) * 60 + float(bits[1])
            elif len(bits) == 3:
                sec = int(bits[0]) * 3600 + int(bits[1]) * 60 + float(bits[2])
            else:
                raise ValueError()
        except (ValueError, OverflowError) as e:
            raise ValueError('开始时间格式应为 01:20.0 或 80.0') from e
    sec = norm_tenth(sec)
    if sec < 0:
        raise ValueError('开始时间不能为负数')
    return sec


def time_label(seconds: float) -> str:
    tenths = max(0, int(round(seconds * 10)))
    m, remain = divmod(tenths, 600)
    s, tenth = divmod(remain, 10)
    return f'{m:02d}:{s:02d}.{tenth}'


def output_name(value: str) -> str:
    value = str(value or '').strip()
    if not value or value in {'.', '..'} or value[-1] in '. ':
        raise ValueError('输出文件名为空或不合法')
    if any(c in '<>:"/\\|?*\x00' or ord(c) < 32 for c in value):
        raise ValueError('输出文件名有 Windows 不支持的字符')
    if not value.lower().endswith('.mp3'):
        raise ValueError('输出必须使用 .mp3 后缀')
    prefix = value.split('.')[0].casefold()
    if prefix in {'con', 'prn', 'aux', 'nul'} | {f'com{i}' for i in range(1, 10)} | {f'lpt{i}' for i in range(1, 10)}:
        raise ValueError('Windows 保留文件名不可用')
    return value


def suggested_name(source: Path) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', source.stem).strip('. ') or 'clip'
    return stem + '_片段.mp3'


def proc_flags(low: bool = False) -> int:
    if os.name == 'nt':
        return getattr(subprocess, 'CREATE_NO_WINDOW', 0) | (getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0) if low else 0)
    return 0


def get_tools(custom: str = '') -> tuple[str, str, str | None]:
    here = Path(__file__).resolve().parent
    candidates = [custom] if custom else []
    for folder in (here / 'bin', here):
        candidates.extend([str(folder / 'ffmpeg.exe'), str(folder / 'ffmpeg')])
    found = shutil.which('ffmpeg')
    if found:
        candidates.append(found)
    for path_str in candidates:
        if not path_str:
            continue
        path = Path(path_str.strip('"')).expanduser()
        if not path.is_file() or path.name.casefold() not in {'ffmpeg', 'ffmpeg.exe'}:
            continue
        ext = '.exe' if path.suffix.casefold() == '.exe' else ''
        ffprobe = path.with_name('ffprobe' + ext)
        ffplay = path.with_name('ffplay' + ext)
        probe = str(ffprobe) if ffprobe.is_file() else shutil.which('ffprobe')
        play = str(ffplay) if ffplay.is_file() else shutil.which('ffplay')
        if probe:
            return str(path), probe, play
    raise RuntimeError('未找到 FFmpeg / FFprobe。请安装并加入 PATH，或将其放在 bin 文件夹。')


def process(args: list[str], *, timeout: float, stop: Callable[[], bool] | None = None,
            low: bool = False) -> tuple[int, bytes, bytes]:
    """Nonblocking to Tk; poll subprocess for cancellation and timeouts in worker only."""
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=proc_flags(low))
    start = time.monotonic()
    try:
        while True:
            if stop and stop():
                raise Cancelled('任务已取消')
            if time.monotonic() - start > timeout:
                raise TimeoutError(f'操作超过 {int(timeout)} 秒，已终止相应 FFmpeg 进程')
            try:
                out, err = proc.communicate(timeout=.15)
                return proc.returncode or 0, out, err
            except subprocess.TimeoutExpired:
                pass
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pass


def read_duration(ffprobe: str, source: Path, stop: Callable[[], bool] | None = None) -> float:
    rc, stdout, stderr = process([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(source)], timeout=13, stop=stop, low=True)
    if rc != 0:
        raise RuntimeError((stderr.decode('utf-8', 'replace') or '无法读取音频').strip()[-180:])
    try:
        d = float(stdout.strip())
    except ValueError as e:
        raise RuntimeError('无法解析音频时长') from e
    if not math.isfinite(d) or d <= 0:
        raise RuntimeError('音频时长无效')
    return d


def decode_wave(ffmpeg: str, source: Path, stop: Callable[[], bool]) -> list[float]:
    """Optional, low CPU, bounded ~41 KB waveform via a C-level FFmpeg filter.

    For speed, resample the visualization to 2 kHz, use one codec/filter thread,
    reduce to 640x64 grayscale, not precise full-frequency peaks. Audio exports unaffected.
    """
    filt = (f'[0:a:0]aresample=2000,aformat=channel_layouts=mono,'
            f'showwavespic=s={WAVE_WIDTH}x{WAVE_HEIGHT}:colors=white:filter=peak:draw=full[w]')
    rc, out, err = process([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error',
        '-threads', '1', '-filter_threads', '1', '-i', str(source), '-filter_complex', filt,
        '-map', '[w]', '-frames:v', '1', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'],
        timeout=40, stop=stop, low=True)
    if rc != 0:
        raise RuntimeError(err.decode('utf-8', 'replace')[-220:] or '波形解析失败')
    if len(out) != WAVE_WIDTH * WAVE_HEIGHT:
        raise RuntimeError('生成的波形数据大小异常')
    peaks = [0.0] * WAVE_WIDTH
    half = WAVE_HEIGHT // 2
    for y in range(WAVE_HEIGHT):
        if y % 8 == 0 and stop():
            raise Cancelled()
        strength = abs(y - half) / max(half, 1)
        for x in range(WAVE_WIDTH):
            if out[y * WAVE_WIDTH + x] > 75 and strength > peaks[x]:
                peaks[x] = strength
    top = max(peaks) or 1.0
    return [min(1.0, (v / top) ** .7) for v in peaks]


@dataclass
class Clip:
    filename: str
    source: Path
    start: float = 0.0
    length: float = DEFAULT_LENGTH
    output: str = ''
    state: str = '待处理'
    note: str = ''
    uid: int = field(default_factory=lambda: next(SERIAL))


def source_from_excel(name: str, audio_dir: Path | None, excel: Path) -> Path:
    name = name.strip()
    path = Path(name).expanduser()
    base = re.split(r'[/\\]', name)[-1]
    paths = []
    if audio_dir:
        paths.append(audio_dir / base)
    paths.extend([excel.parent / path, excel.parent / 'input' / base, path])
    for candidate in paths:
        if candidate.is_file():
            return candidate.resolve()
    return paths[0] if paths else path


def load_excel(path: Path, audio_dir: Path | None) -> list[Clip]:
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book['裁剪任务'] if '裁剪任务' in book.sheetnames else book.active
        itr = sheet.iter_rows(values_only=True)
        top = next(itr, None)
        if not top:
            raise ValueError('Excel 表格为空')
        normalized = [str(v or '').casefold().replace(' ', '').replace('_', '') for v in top]
        indices = []
        for aliases in ALIASES:
            allowed = {a.casefold().replace(' ', '').replace('_', '') for a in aliases}
            hits = [i for i, name in enumerate(normalized) if name in allowed]
            if len(hits) != 1:
                raise ValueError('Excel 首行必须有四列：' + '、'.join(HEADERS))
            indices.append(hits[0])
        clips = []
        for row_no, row in enumerate(itr, 2):
            vals = [row[i] if i < len(row) else None for i in indices]
            if all(v is None or str(v).strip() == '' for v in vals):
                continue
            try:
                filename = str(vals[0] or '').strip()
                if not filename:
                    raise ValueError('原始文件名不能为空')
                start = timestamp(vals[1])
                length = norm_tenth(float(vals[2]))
                if not MIN_LENGTH <= length <= MAX_LENGTH:
                    raise ValueError('裁剪时长必须在 0.1～40 秒')
                result = output_name(vals[3])
                source = source_from_excel(filename, audio_dir, path)
                clip = Clip(filename, source, start, length, result)
                if not source.is_file():
                    clip.state, clip.note = '缺少音频', '未找到对应音频文件'
                clips.append(clip)
            except (ValueError, TypeError, OverflowError) as e:
                raise ValueError(f'Excel 第 {row_no} 行：{e}') from e
        return clips
    finally:
        book.close()


def save_excel(path: Path, clips: list[Clip], audio_root: Path | None) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = '裁剪任务'
    sheet.append(HEADERS)
    from openpyxl.styles import Font, PatternFill, Alignment
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='204A79')
        cell.alignment = Alignment(horizontal='center')
    sheet.freeze_panes = 'A2'
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 25
    for c in clips:
        filename = c.filename
        if c.source.is_file():
            if audio_root and c.source.parent.resolve() == audio_root.resolve():
                filename = c.source.name
            else:
                filename = os.path.relpath(c.source, path.parent)
        sheet.append([filename, time_label(c.start), norm_tenth(c.length), c.output])
        sheet.cell(sheet.max_row, 2).number_format = '@'
    for label, width in zip('ABCD', (48, 20, 17, 40)):
        sheet.column_dimensions[label].width = width
    sheet.auto_filter.ref = f'A1:D{max(sheet.max_row, 2)}'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.musiccutter_', suffix='.xlsx', dir=path.parent)
    os.close(fd)
    temp = Path(tmp)
    try:
        book.save(temp)
        os.replace(temp, path)
    finally:
        book.close()
        temp.unlink(missing_ok=True)


def export_one(clip: Clip, folder: Path, tools: tuple[str, str, str | None], overwrite: bool,
               stop: Callable[[], bool], stage: Callable[[str], None], duration: float | None = None) -> str:
    filename = output_name(clip.output)
    if not clip.source.is_file():
        raise FileNotFoundError(f'找不到文件：{clip.source}')
    if clip.start < 0 or not MIN_LENGTH <= clip.length <= MAX_LENGTH:
        raise ValueError('剪辑参数超出范围')
    if duration is None:
        duration = read_duration(tools[1], clip.source, stop)
    if clip.start + clip.length > duration + .04:
        raise ValueError(f'片段超出原曲时长 {time_label(duration)}')
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / filename
    if clip.source.resolve() == dest.resolve():
        raise ValueError('输出文件不能覆盖原始音频')
    if dest.exists() and not overwrite:
        return '跳过（已存在）'
    handle, name = tempfile.mkstemp(prefix='.musiccutter_', suffix='.mp3', dir=folder)
    os.close(handle)
    temp = Path(name)
    try:
        stage('剪辑 / 编码 MP3')
        rc, _, err = process([tools[0], '-hide_banner', '-nostdin', '-v', 'error', '-y',
            '-threads', '1', '-ss', f'{clip.start:.1f}', '-i', str(clip.source), '-t', f'{clip.length:.1f}',
            '-map', '0:a:0', '-vn', '-sn', '-map_metadata', '-1', '-c:a', 'libmp3lame',
            '-b:a', '192k', '-f', 'mp3', str(temp)], timeout=75, stop=stop, low=True)
        if rc:
            raise RuntimeError(err.decode('utf-8', 'replace')[-220:] or 'FFmpeg 裁剪失败')
        if temp.stat().st_size < 128:
            raise RuntimeError('输出文件无效或为空')
        stage('验证片段')
        actual = read_duration(tools[1], temp, stop)
        if abs(actual - clip.length) > .65:
            raise RuntimeError(f'输出片段时长异常：{actual:.1f} 秒')
        if stop():
            raise Cancelled()
        os.replace(temp, dest)
        return '成功'
    finally:
        temp.unlink(missing_ok=True)


def batch_export(clips: list[Clip], folder: Path, tools: tuple[str, str, str | None],
                 overwrite: bool, cancel: threading.Event,
                 progress: Callable[[str, Any], None]) -> tuple[int, int, int, Path, bool]:
    used: set[str] = set()
    durations: dict[Path, float] = {}
    results: list[tuple[Clip, str, str]] = []
    canceled = False
    for index, clip in enumerate(clips, 1):
        if cancel.is_set():
            canceled = True
            break
        try:
            key = clip.output.casefold()
            if key in used:
                raise ValueError('输出文件名重复')
            used.add(key)
            progress('export_stage', (index, len(clips), clip.uid, '检查源文件'))
            dur = durations.get(clip.source)
            if dur is None:
                dur = read_duration(tools[1], clip.source, cancel.is_set)
                durations[clip.source] = dur
            result = export_one(clip, folder, tools, overwrite, cancel.is_set,
                                lambda s: progress('export_stage', (index, len(clips), clip.uid, s)), dur)
            detail = str(folder / clip.output) if result == '成功' else '已有文件，未覆盖'
        except Cancelled:
            canceled = True
            break
        except Exception as e:
            result, detail = '失败', str(e)
        results.append((clip, result, detail))
        progress('export_row', (index, len(clips), clip.uid, result, detail))
    folder.mkdir(parents=True, exist_ok=True)
    report = folder / ('处理报告_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.csv')
    with report.open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['歌曲', '开始时间', '截取秒数', '输出文件', '状态', '详情'])
        for c, status, details in results:
            writer.writerow([c.filename, time_label(c.start), f'{c.length:.1f}', c.output, status, details])
    return (sum(s == '成功' for _, s, _ in results),
            sum(s.startswith('跳过') for _, s, _ in results),
            sum(s == '失败' for _, s, _ in results), report, canceled)


class MusicCutterApp:
    """All Tk calls happen on GUI thread. Heavy operations run in workers."""
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('MusicCutter v3.0 · 极速稳定版')
        root.geometry('1190x800')
        root.minsize(980, 680)
        root.configure(background=BG)
        self.tools: tuple[str, str, str | None] | None = None
        self.audio_root: Path | None = None
        self.excel_path: Path | None = None
        self.output_dir = Path(__file__).resolve().parent / 'output'
        self.clips: list[Clip] = []
        self.by_id: dict[int, Clip] = {}
        self.selected: int | None = None
        self.duration_cache: dict[Path, float] = {}
        self.wave_cache: OrderedDict[Path, list[float]] = OrderedDict()
        self.wave_peaks: list[float] = []
        self.track_duration = 0.0
        self.auto_wave = tk.BooleanVar(value=False)  # FIRST PRINCIPLE: don't decode 50 tracks by default.
        self.overwrite = tk.BooleanVar(value=False)
        self.start_var = tk.StringVar(value='00:00.0')
        self.length_var = tk.StringVar(value='15.0')
        self.name_var = tk.StringVar()
        self.song_var = tk.StringVar(value='先添加歌曲，再在左侧选择歌曲')
        self.wave_var = tk.StringVar(value='极速模式：选中歌曲只读取时长。点击「加载波形」后才解码。')
        self.preview_var = tk.StringVar(value='试听：尚未开始')
        self.editor_var = tk.StringVar(value='当前片段的修改尚未写入 Excel')
        self.status_var = tk.StringVar(value='准备就绪')
        self.busy_var = tk.StringVar(value='后台任务：无')
        self.health_var = tk.StringVar(value='界面响应：正常')
        self.logs: deque[str] = deque(maxlen=180)
        self.ffmpeg_var = tk.StringVar(value='检测 FFmpeg…')
        self.output_var = tk.StringVar(value=str(self.output_dir))
        self.event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.last_heartbeat = time.monotonic()
        self.selection_seq = 0
        self.wave_seq = 0
        self._debounce_id = None
        self.wave_condition = threading.Condition()
        self.wave_pending: tuple[int, int, Path, bool] | None = None
        self.wave_stop = threading.Event()
        self.wave_shutdown = False
        self.preview_condition = threading.Condition()
        self.preview_pending: tuple[int, Path, float, float] | None = None
        self.preview_seq = 0
        self.preview_shutdown = False
        self.preview_proc: subprocess.Popen | None = None
        self.preview_started = 0.0
        self.preview_length = 0.0
        self.preview_active = False
        self.io_busy = False
        self.saving = False
        self.exporting = False
        self.cancel_export = threading.Event()
        self._silence_tree = False
        self._dirty = False
        self.revision = 0  # track edits made during background Excel save
        self.wave_working = False
        self.preview_working = False
        self.drag_anchor: float | None = None
        self._wave_drawn_for: tuple[int, int] | None = None
        self._style()
        self._build_ui()
        self._set_tools()
        threading.Thread(target=self._wave_worker, daemon=True, name='mc-wave').start()
        threading.Thread(target=self._preview_worker, daemon=True, name='mc-preview').start()
        root.after(80, self._drain_events)
        root.after(250, self._heartbeat)
        root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _style(self) -> None:
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('M.TFrame', background=BG)
        s.configure('P.TFrame', background=PAPER)
        s.configure('M.TLabel', background=BG, foreground=INK, font=('Arial', 10))
        s.configure('P.TLabel', background=PAPER, foreground=INK, font=('Arial', 10))
        s.configure('Quiet.TLabel', background=PAPER, foreground=GRAY, font=('Arial', 9))
        s.configure('Title.TLabel', background=BG, foreground=INK, font=('Arial', 18, 'bold'))
        s.configure('H.TLabel', background=PAPER, foreground=INK, font=('Arial', 11, 'bold'))
        s.configure('TButton', padding=(9, 6))
        s.configure('Accent.TButton', background=ACCENT, foreground='white', padding=(12, 8))
        s.configure('Treeview', rowheight=27)
        s.configure('Treeview.Heading', font=('Arial', 10, 'bold'))
        s.map('Treeview', foreground=[('selected', INK)], background=[('selected', '#e0eaff')])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style='M.TFrame', padding=14)
        outer.pack(fill='both', expand=True)
        head = ttk.Frame(outer, style='M.TFrame')
        head.pack(fill='x', pady=(0, 12))
        ttk.Label(head, text='MusicCutter', style='Title.TLabel').pack(side='left')
        ttk.Label(head, text='v3.0  ·  极速优先 / 单任务波形 / 后台剪辑', style='M.TLabel').pack(side='left', padx=12)
        ttk.Label(head, textvariable=self.ffmpeg_var, style='M.TLabel').pack(side='right')
        tools = ttk.Frame(outer, style='M.TFrame')
        tools.pack(fill='x', pady=(0, 10))
        self.import_button = ttk.Button(tools, text='添加音频', command=self._pick_files)
        self.import_button.pack(side='left', padx=(0, 6))
        self.folder_button = ttk.Button(tools, text='添加文件夹', command=self._pick_folder)
        self.folder_button.pack(side='left', padx=(0, 6))
        self.open_button = ttk.Button(tools, text='打开 Excel', command=self._open_excel)
        self.open_button.pack(side='left', padx=(0, 6))
        self.save_button = ttk.Button(tools, text='保存 Excel', command=self._save_excel)
        self.save_button.pack(side='left')
        ttk.Button(tools, text='指定 FFmpeg', command=self._select_tools).pack(side='right')

        area = ttk.Panedwindow(outer, orient='horizontal')
        area.pack(fill='both', expand=True)
        left = ttk.Frame(area, style='P.TFrame', padding=12)
        right = ttk.Frame(area, style='P.TFrame', padding=12)
        area.add(left, weight=2)
        area.add(right, weight=4)
        ttk.Label(left, text='歌曲 / 片段任务', style='H.TLabel').pack(anchor='w', pady=(0, 8))
        holder = ttk.Frame(left, style='P.TFrame')
        holder.pack(fill='both', expand=True)
        cols = ('name', 'start', 'length', 'state')
        self.tree = ttk.Treeview(holder, columns=cols, show='headings', selectmode='browse')
        for key, title, width in [('name', '歌曲', 160), ('start', '开始', 78), ('length', '秒', 46), ('state', '状态', 75)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=42, stretch=(key == 'name'))
        scr = ttk.Scrollbar(holder, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scr.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scr.pack(side='right', fill='y')
        self.tree.tag_configure('error', foreground=ERROR)
        self.tree.tag_configure('ok', foreground=SUCCESS)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        toolrow = ttk.Frame(left, style='P.TFrame')
        toolrow.pack(fill='x', pady=(10, 0))
        ttk.Button(toolrow, text='复制片段', command=self._clone).pack(side='left', padx=(0, 5))
        ttk.Button(toolrow, text='移除片段', command=self._delete).pack(side='left')
        ttk.Button(toolrow, text='定位源文件', command=self._relink).pack(side='right')

        top = ttk.Frame(right, style='P.TFrame')
        top.pack(fill='x')
        ttk.Label(top, text='片段选择与试听', style='H.TLabel').pack(side='left')
        self.wave_button = ttk.Button(top, text='加载波形（可选）', command=self._show_wave)
        self.wave_button.pack(side='right', padx=(5, 0))
        ttk.Button(top, text='取消解析', command=self._cancel_wave).pack(side='right')
        ttk.Checkbutton(right, text='自动加载波形（可能较慢；默认关闭）', variable=self.auto_wave,
                        command=self._wave_toggle).pack(anchor='w', pady=(7, 2))
        ttk.Label(right, textvariable=self.song_var, style='Quiet.TLabel', wraplength=660).pack(anchor='w', pady=(0, 3))
        ttk.Label(right, textvariable=self.wave_var, style='Quiet.TLabel', wraplength=660).pack(anchor='w', pady=(0, 7))
        self.canvas = tk.Canvas(right, height=142, bg='#f7f9ff', highlightthickness=1, highlightbackground='#dfe5f2')
        self.canvas.pack(fill='x', pady=(0, 10))
        self.canvas.bind('<Configure>', lambda _e: self._draw_wave())
        self.canvas.bind('<Button-1>', self._drag_begin)
        self.canvas.bind('<B1-Motion>', self._drag_move)
        self.canvas.bind('<ButtonRelease-1>', self._drag_end)
        edit = ttk.Frame(right, style='P.TFrame')
        edit.pack(fill='x', pady=(2, 10))
        ttk.Label(edit, text='开始 (分:秒.1)', style='P.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(edit, text='截取秒数', style='P.TLabel').grid(row=0, column=1, sticky='w', padx=10)
        ttk.Label(edit, text='输出文件名', style='P.TLabel').grid(row=0, column=2, sticky='w')
        self.start_entry = ttk.Entry(edit, textvariable=self.start_var, width=13)
        self.start_entry.grid(row=1, column=0, sticky='ew')
        self.length_entry = ttk.Spinbox(edit, from_=MIN_LENGTH, to=MAX_LENGTH, increment=.1,
                                        textvariable=self.length_var, width=10, format='%.1f')
        self.length_entry.grid(row=1, column=1, padx=10, sticky='ew')
        ttk.Entry(edit, textvariable=self.name_var).grid(row=1, column=2, sticky='ew')
        edit.columnconfigure(2, weight=1)
        action = ttk.Frame(right, style='P.TFrame')
        action.pack(fill='x')
        ttk.Button(action, text='保存当前片段设置', command=self._apply).pack(side='left', padx=(0, 6))
        self.listen_button = ttk.Button(action, text='▶ 试听片段', command=self._start_preview, style='Accent.TButton')
        self.listen_button.pack(side='left', padx=(0, 6))
        ttk.Button(action, text='■ 停止试听', command=self._stop_preview).pack(side='left')
        ttk.Label(right, textvariable=self.editor_var, style='Quiet.TLabel').pack(anchor='w', pady=(12, 3))
        ttk.Label(right, textvariable=self.preview_var, style='Quiet.TLabel').pack(anchor='w')

        foot = ttk.Frame(outer, style='P.TFrame', padding=12)
        foot.pack(fill='x', pady=(11, 0))
        row = ttk.Frame(foot, style='P.TFrame')
        row.pack(fill='x')
        ttk.Label(row, text='输出文件夹', style='P.TLabel').pack(side='left', padx=(0, 8))
        ttk.Label(row, textvariable=self.output_var, style='Quiet.TLabel').pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='选择目录', command=self._pick_output).pack(side='right')
        ttk.Checkbutton(row, text='覆盖已有输出', variable=self.overwrite).pack(side='right', padx=9)
        exports = ttk.Frame(foot, style='P.TFrame')
        exports.pack(fill='x', pady=(8, 4))
        self.export_button = ttk.Button(exports, text='开始批量导出 MP3', command=self._export,
                                        style='Accent.TButton')
        self.export_button.pack(side='left')
        self.cancel_button = ttk.Button(exports, text='取消导出', command=self._cancel_export, state='disabled')
        self.cancel_button.pack(side='left', padx=8)
        self.progress = ttk.Progressbar(exports, mode='determinate', maximum=100)
        self.progress.pack(side='left', fill='x', expand=True, padx=8)
        self.progress_label = tk.StringVar(value='进度：0/0')
        ttk.Label(exports, textvariable=self.progress_label, style='Quiet.TLabel').pack(side='right')
        ttk.Label(foot, textvariable=self.busy_var, style='P.TLabel', wraplength=1050).pack(anchor='w', pady=(4, 3))
        ttk.Label(foot, textvariable=self.status_var, style='Quiet.TLabel', wraplength=1050).pack(anchor='w')
        diagnostic = ttk.Frame(foot, style='P.TFrame')
        diagnostic.pack(fill='x', pady=(7, 2))
        ttk.Label(diagnostic, text='运行日志（最近记录）', style='P.TLabel').pack(side='left')
        ttk.Label(diagnostic, textvariable=self.health_var, style='Quiet.TLabel').pack(side='left', padx=12)
        ttk.Button(diagnostic, text='复制诊断信息', command=self._copy_diagnostics).pack(side='right')
        ttk.Button(diagnostic, text='显示/隐藏日志', command=self._toggle_logs).pack(side='right', padx=5)
        self.log_view = tk.Text(foot, height=3, wrap='word', bg='#f7f9ff', fg=GRAY,
                                relief='flat', state='disabled', font=('Consolas', 9))
        self.log_open = False
        self._log('程序已启动。极速模式默认关闭自动波形解析。')
        self._draw_wave()

    def _toggle_logs(self) -> None:
        self.log_open = not self.log_open
        if self.log_open:
            self.log_view.pack(fill='x')
        else:
            self.log_view.pack_forget()

    def _log(self, line: str) -> None:
        entry = f'[{datetime.now():%H:%M:%S}] {line}'
        self.logs.append(entry)
        self.log_view['state'] = 'normal'
        self.log_view.insert('end', entry + '\n')
        num_lines = int(self.log_view.index('end-1c').split('.')[0])
        if num_lines > 180:
            self.log_view.delete('1.0', f'{num_lines-180}.0')
        self.log_view.see('end')
        self.log_view['state'] = 'disabled'

    def _copy_diagnostics(self) -> None:
        status = ['MusicCutter v' + APP_VERSION,
                  'OS: ' + platform.platform(),
                  'Python: ' + sys.version.splitlines()[0],
                  'FFmpeg配置: ' + ('正常' if self.tools else '未配置'),
                  '歌曲数: ' + str(len(self.clips)),
                  '当前界面状态: ' + self.health_var.get(),
                  '后台任务: ' + self.busy_var.get(),
                  '最新提示: ' + self.status_var.get(), '', '最近日志：', *self.logs]
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append('\n'.join(status))
            self.status_var.set('诊断信息已复制到剪贴板，可以发送给开发者排查问题。')
        except tk.TclError:
            self.status_var.set('无法复制诊断信息，请截屏运行日志。')

    def _set_tools(self, path: str = '') -> None:
        try:
            self.tools = get_tools(path)
            self.ffmpeg_var.set('FFmpeg ✓  |  FFplay ' + ('✓' if self.tools[2] else '未安装'))
            self._log('FFmpeg 和 FFprobe 已就绪；试听 ' + ('可用' if self.tools[2] else '不可用（缺少 FFplay）'))
        except Exception:
            self.tools = None
            self.ffmpeg_var.set('FFmpeg 未配置')
            self._log('FFmpeg 未配置，请使用右上角按钮选择 FFmpeg 路径。')
            self.status_var.set('请点击右上角「指定 FFmpeg」或安装 FFmpeg 后重启。')

    def _select_tools(self) -> None:
        path = filedialog.askopenfilename(title='选择 ffmpeg 或 ffmpeg.exe', filetypes=[('全部文件', '*')])
        if path:
            self._set_tools(path)
            if not self.tools:
                messagebox.showerror('设置失败', '请确认所选为 ffmpeg，且 ffprobe 在同目录或 PATH。')

    def _emit(self, kind: str, payload: Any = None) -> None:
        self.event_queue.put((kind, payload))

    def _job(self, name: str, description: str = '', active: bool = True) -> None:
        if active:
            if name not in self.jobs:
                self.jobs[name] = {'since': time.monotonic(), 'last': time.monotonic(), 'text': description}
            else:
                self.jobs[name]['last'] = time.monotonic()
                self.jobs[name]['text'] = description
        else:
            self.jobs.pop(name, None)
        self._render_jobs()

    def _render_jobs(self) -> None:
        if not self.jobs and not (self.wave_working or self.preview_working):
            self.busy_var.set('后台任务：无 · 界面可操作')
            return
        now = time.monotonic()
        rows = []
        for name, job in self.jobs.items():
            elapsed = int(now - job['since'])
            stalled = now - job['last'] > 12
            warn = '（超过 12 秒未更新，可能较慢）' if stalled else ''
            rows.append(f"{job['text']} [{elapsed}s]{warn}")
        if self.wave_working and 'wave' not in self.jobs:
            rows.append('波形子进程正在取消/收尾中')
        if self.preview_working and 'preview' not in self.jobs:
            rows.append('播放器子进程正在停止中')
        self.busy_var.set('后台任务：' + ' | '.join(rows))

    def _heartbeat(self) -> None:
        now = time.monotonic()
        delay = now - self.last_heartbeat
        self.last_heartbeat = now
        if delay > 1.6:
            self.health_var.set(f'⚠ 界面曾延迟 {delay:.1f}s')
            self._log(f'界面心跳延迟 {delay:.1f}s；建议关闭自动波形解析或将歌曲复制到本地硬盘。')
            self.status_var.set(f'⚠ 检测到界面响应延迟 {delay:.1f}s；建议关闭自动加载波形。')
        else:
            self.health_var.set('界面响应：正常 ✓')
        self._render_jobs()
        if self.preview_active:
            sec = max(0., min(self.preview_length, now - self.preview_started))
            self.preview_var.set(f'FFplay 已启动 · 预计播放进度 {sec:.1f}/{self.preview_length:.1f}s（仅为计时参考）')
        self.root.after(250, self._heartbeat)

    def _drain_events(self) -> None:
        for _ in range(12):  # bounded work per frame prevents event flood from hanging Tk
            try:
                kind, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._event(kind, payload)
        self.root.after(70, self._drain_events)

    def _event(self, kind: str, x: Any) -> None:
        if kind == 'imported':
            clips, folder = x
            self.io_busy = False
            self._job('import', active=False)
            self._io_buttons()
            if folder:
                self.audio_root = folder
            self._append_clips(clips)
            self._log(f'音频文件扫描完成，导入 {len(clips)} 个候选文件')
        elif kind == 'relink_done':
            links, folder = x
            self.io_busy = False
            self._job('import', active=False)
            self._io_buttons()
            self.audio_root = folder
            count = 0
            for uid, path in links:
                clip = self.by_id.get(uid)
                if clip is not None:
                    clip.source, clip.state = path, '待处理'
                    self._update_row(clip)
                    count += 1
            if count:
                self.revision += 1
                self._dirty = True
            self.status_var.set(f'重新定位完成：找到 {count} 个原本缺失的音频文件。')
            if self._current():
                self._cancel_wave(quiet=True)
                self.track_duration = 0.0
                self.wave_peaks = []
                self._request_media()
        elif kind == 'excel_loaded':
            clips, path = x
            self.io_busy = False
            self._job('import', active=False)
            self._io_buttons()
            self._stop_preview(quiet=True)
            self._cancel_wave(quiet=True)
            self.clips, self.by_id = clips, {c.uid: c for c in clips}
            self.audio_root = self.audio_root or (path.parent / 'input')
            self.excel_path, self.output_dir = path, path.parent / 'output'
            self.output_var.set(str(self.output_dir))
            self.duration_cache.clear()
            self.wave_cache.clear()
            self._rebuild_tree()
            self.revision += 1
            self._dirty = False
            self.status_var.set(f'Excel 加载完成：{len(clips)} 条。缺失源文件 {sum(c.state == "缺少音频" for c in clips)} 条。')
            self._log(f'已打开 Excel：{path.name}，记录 {len(clips)} 条')
        elif kind == 'io_error':
            self.io_busy = False
            self._job('import', active=False)
            self._io_buttons()
            self.status_var.set('读取失败：' + str(x))
            self._log('读取失败：' + str(x))
        elif kind == 'save_done':
            saved_path, revision = x
            self.saving = False
            self._job('save', active=False)
            self._io_buttons()
            self.excel_path = saved_path
            self._dirty = self.revision != revision
            self.editor_var.set('Excel 已保存 ✓' if not self._dirty else 'Excel 已保存，但保存期间又做了修改，请再次保存')
            self.status_var.set('已写入 Excel：' + str(saved_path) + ('（有新的未保存修改）' if self._dirty else ''))
            self._log('Excel 已写入：' + str(saved_path))
        elif kind == 'save_error':
            self.saving = False
            self._job('save', active=False)
            self._io_buttons()
            self.status_var.set('Excel 保存失败：' + str(x))
            self._log('Excel 保存失败：' + str(x))
        elif kind == 'media_stage':
            selection, wave_id, detail = x
            if selection == self.selection_seq and wave_id == self.wave_seq:
                self.wave_var.set(detail)
                self._job('wave', detail)
                self._log('音频任务：' + detail)
        elif kind == 'media_duration':
            selection, wave_id, path, duration = x
            self.duration_cache[path] = duration
            if selection == self.selection_seq and wave_id == self.wave_seq:
                self.track_duration = duration
                self._draw_wave()
                self.song_var.set(f'{path.name} · 总时长 {time_label(duration)}')
        elif kind == 'media_wave':
            selection, wave_id, path, peaks = x
            self.wave_cache[path] = peaks
            self.wave_cache.move_to_end(path)
            while len(self.wave_cache) > MAX_WAVE_CACHE:
                self.wave_cache.popitem(last=False)
            if selection == self.selection_seq and wave_id == self.wave_seq:
                self.wave_peaks = peaks
                self.wave_var.set('波形已就绪 · 可以拖动选区')
                self._log('波形解析完成：' + path.name)
                self._draw_wave()
                self._job('wave', active=False)
        elif kind == 'media_done':
            selection, wave_id, path = x
            if selection == self.selection_seq and wave_id == self.wave_seq:
                if not self.wave_peaks:
                    self.wave_var.set('时长就绪 · 极速模式（波形未加载）')
                self._job('wave', active=False)
        elif kind == 'media_error':
            selection, wave_id, error = x
            if selection == self.selection_seq and wave_id == self.wave_seq:
                self.wave_var.set('音频解析失败：' + str(error)[:100])
                self._log('音频解析失败：' + str(error)[:140])
                self._job('wave', active=False)
        elif kind == 'preview_stage':
            seq, desc = x
            if seq == self.preview_seq:
                self.preview_var.set(desc)
                self._job('preview', desc)
        elif kind == 'preview_started':
            seq, length = x
            if seq == self.preview_seq:
                self.preview_active = True
                self.preview_started = time.monotonic()
                self.preview_length = length
                self.preview_var.set('正在播放音乐 · FFplay 已启动（声音可能略延迟）')
                self._log('已启动试听，片段时长 ' + f'{length:.1f}s')
                self._job('preview', '正在试听片段')
        elif kind == 'preview_finished':
            seq, rc, err = x
            if seq == self.preview_seq:
                self.preview_active = False
                self._job('preview', active=False)
                if rc == 0:
                    self.preview_var.set('试听结束 ✓')
                    self._log('试听已正常结束')
                else:
                    self.preview_var.set('试听出错：' + (err or f'FFplay 退出码 {rc}')[:120])
                    self._log('试听失败：' + (err or f'退出码 {rc}')[:160])
        elif kind == 'preview_error':
            seq, error = x
            if seq == self.preview_seq:
                self.preview_active = False
                self._job('preview', active=False)
                self.preview_var.set('无法试听：' + str(error)[:120])
                self._log('无法启动试听：' + str(error)[:160])
        elif kind == 'export_stage':
            i, count, uid, stage = x
            self._job('export', f'{i}/{count} · {stage} · {self.by_id[uid].filename if uid in self.by_id else ""}')
        elif kind == 'export_row':
            i, count, uid, status, detail = x
            if uid in self.by_id:
                self.by_id[uid].state, self.by_id[uid].note = status, detail
                self._update_row(self.by_id[uid])
            self.progress['value'] = i * 100 / max(1, count)
            self.progress_label.set(f'进度：{i}/{count}')
            self._job('export', f'{i}/{count} · {status}')
            self._log(f'导出进度 {i}/{count}：{status}' + (' | ' + str(detail)[:90] if status == '失败' else ''))
        elif kind == 'export_done':
            self.exporting = False
            self._job('export', active=False)
            self._io_buttons()
            yes, skipped, no, report, canceled = x
            self.status_var.set(f'批量导出{"已取消" if canceled else "完成"}：成功 {yes}，跳过 {skipped}，失败 {no}。报告：{report}')
            self.cancel_button['state'] = 'disabled'
            self._log('导出结束：成功 ' + str(yes) + '，跳过 ' + str(skipped) + '，失败 ' + str(no))
        elif kind == 'export_error':
            self.exporting = False
            self._job('export', active=False)
            self._io_buttons()
            self.cancel_button['state'] = 'disabled'
            self.status_var.set('导出失败：' + str(x))
            self._log('导出失败：' + str(x))

    def _io_buttons(self) -> None:
        state = 'disabled' if (self.io_busy or self.saving or self.exporting) else 'normal'
        for b in (self.import_button, self.folder_button, self.open_button, self.save_button, self.export_button):
            b['state'] = state

    def _append_clips(self, new: list[Clip]) -> None:
        existing = {str(c.source) for c in self.clips}
        names = {c.output.casefold() for c in self.clips}
        added = []
        for c in new:
            if str(c.source) in existing:
                continue
            existing.add(str(c.source))
            c.output = suggested_name(c.source)
            base = c.output
            n = 2
            while c.output.casefold() in names:
                c.output = Path(base).stem + f'_{n}.mp3'
                n += 1
            names.add(c.output.casefold())
            self.clips.append(c)
            self.by_id[c.uid] = c
            added.append(c)
        if added:
            self.revision += 1
            self._dirty = True
        self._rebuild_tree(select=added[0].uid if added and self.selected is None else self.selected)
        self.status_var.set(f'已添加 {len(added)} 首，当前共 {len(self.clips)} 条。默认只读选中歌曲时长，不批量解析波形。')

    def _rebuild_tree(self, select: int | None = None) -> None:
        self._silence_tree = True
        self.tree.delete(*self.tree.get_children())
        for clip in self.clips:
            self._insert_row(clip)
        self._silence_tree = False
        if select and self.tree.exists(str(select)):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))
            self._on_select(None)
        else:
            self.selected = None
            self._cancel_wave(quiet=True)
            self.song_var.set('请从左侧选择歌曲')
            self.wave_peaks = []
            self.track_duration = 0
            self._draw_wave()

    def _insert_row(self, c: Clip) -> None:
        self.tree.insert('', 'end', iid=str(c.uid), values=(c.source.name, time_label(c.start), f'{c.length:.1f}', c.state),
                         tags=('error',) if c.state in {'失败', '缺少音频'} else ('ok',) if c.state == '成功' else ())

    def _update_row(self, c: Clip) -> None:
        if self.tree.exists(str(c.uid)):
            self.tree.item(str(c.uid), values=(c.source.name, time_label(c.start), f'{c.length:.1f}', c.state),
                           tags=('error',) if c.state in {'失败', '缺少音频'} else ('ok',) if c.state == '成功' else ())

    def _current(self) -> Clip | None:
        return self.by_id.get(self.selected) if self.selected is not None else None

    def _on_select(self, _event: Any) -> None:
        if self._silence_tree:
            return
        selected = self.tree.selection()
        if not selected:
            return
        uid = int(selected[0])
        if uid == self.selected:
            return
        self.selected = uid
        self.selection_seq += 1
        self._stop_preview(quiet=True)
        self._cancel_wave(quiet=True)
        c = self._current()
        if not c:
            return
        self.start_var.set(time_label(c.start))
        self.length_var.set(f'{c.length:.1f}')
        self.name_var.set(c.output)
        self.editor_var.set('编辑后点击「保存当前片段设置」；保存 Excel 才会写入磁盘')
        self.song_var.set(f'当前歌曲：{c.source.name} · 时长待读取')
        self.wave_var.set('等待读取时长（波形按需生成）')
        self.track_duration = self.duration_cache.get(c.source, 0.0)
        self.wave_peaks = self.wave_cache.get(c.source, [])
        self._draw_wave()
        if self._debounce_id is not None:
            self.root.after_cancel(self._debounce_id)
        self._debounce_id = self.root.after(180, self._request_media)

    def _request_media(self, waveform: bool | None = None) -> None:
        self._debounce_id = None
        c = self._current()
        if not c:
            return
        if not self.tools:
            self.wave_var.set('FFmpeg 未配置；无法检查歌曲时长')
            return
        if waveform is None:
            waveform = self.auto_wave.get()
        if self.wave_peaks and self.track_duration:
            self.wave_var.set('波形已从缓存加载')
            return
        if self.track_duration and not waveform:
            self.wave_var.set('时长已加载 · 点击「加载波形」可显示波形')
            return
        self.wave_seq += 1
        request = (self.selection_seq, self.wave_seq, c.source, bool(waveform))
        with self.wave_condition:
            self.wave_pending = request  # only last requested song retained
            self.wave_condition.notify()
        self.wave_var.set('正在排队读取时长' + ('和波形' if waveform else ''))
        self._job('wave', '读取歌曲信息' + (' / 波形' if waveform else ''))

    def _show_wave(self) -> None:
        if self._current():
            self._cancel_wave(quiet=True)
            self._request_media(waveform=True)

    def _wave_toggle(self) -> None:
        if self.auto_wave.get():
            self._show_wave()

    def _cancel_wave(self, quiet: bool = False) -> None:
        self.wave_seq += 1
        with self.wave_condition:
            self.wave_pending = None
            self.wave_condition.notify()
        self._job('wave', active=False)
        if not quiet:
            self.wave_var.set('已请求取消解析（后台结束后状态自动恢复）；其他操作不受影响')

    def _wave_worker(self) -> None:
        while True:
            with self.wave_condition:
                while self.wave_pending is None and not self.wave_shutdown:
                    self.wave_condition.wait()
                if self.wave_shutdown:
                    return
                select_seq, wave_id, path, do_wave = self.wave_pending
                self.wave_pending = None
            self.wave_working = True
            stale = lambda: self.wave_shutdown or select_seq != self.selection_seq or wave_id != self.wave_seq
            try:
                if stale():
                    continue
                if not path.is_file():
                    raise FileNotFoundError('原始音频不存在')
                dur = self.duration_cache.get(path)
                if dur is None:
                    self._emit('media_stage', (select_seq, wave_id, '正在使用 FFprobe 读取时长'))
                    dur = read_duration(self.tools[1], path, stale)  # type: ignore[index]
                if stale():
                    continue
                self._emit('media_duration', (select_seq, wave_id, path, dur))
                if do_wave:
                    peaks = self.wave_cache.get(path)
                    if peaks is None:
                        self._emit('media_stage', (select_seq, wave_id, '正在低负载解析波形（可取消）'))
                        peaks = decode_wave(self.tools[0], path, stale)  # type: ignore[index]
                    if not stale():
                        self._emit('media_wave', (select_seq, wave_id, path, peaks))
                else:
                    self._emit('media_done', (select_seq, wave_id, path))
            except Cancelled:
                continue
            except Exception as e:
                if not stale():
                    self._emit('media_error', (select_seq, wave_id, str(e)))
            finally:
                self.wave_working = False

    def _start_preview(self) -> None:
        c = self._current()
        if not c:
            self.status_var.set('先选中歌曲')
            return
        if not self._apply(quiet=True):
            return
        if not self.tools or not self.tools[2]:
            self.preview_var.set('没有找到 FFplay；需安装包含 ffplay 的 FFmpeg')
            return
        self._stop_preview(quiet=True)
        self._cancel_wave(quiet=True)  # playback takes priority over optional waveform CPU work
        with self.preview_condition:
            self.preview_pending = (self.preview_seq, c.source, c.start, c.length)
            self.preview_condition.notify()
        self.preview_var.set('正在准备试听 · 后台启动 FFplay…')
        self._job('preview', '正在启动音频试听')

    def _stop_preview(self, quiet: bool = False) -> None:
        self.preview_seq += 1
        self.preview_active = False
        with self.preview_condition:
            self.preview_pending = None
            proc = self.preview_proc
            self.preview_condition.notify()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()  # no wait, no GUI blocking
            except OSError:
                pass
        self._job('preview', active=False)
        if not quiet:
            self.preview_var.set('已请求停止试听')

    def _preview_worker(self) -> None:
        while True:
            with self.preview_condition:
                while self.preview_pending is None and not self.preview_shutdown:
                    self.preview_condition.wait()
                if self.preview_shutdown:
                    return
                seq, source, start, length = self.preview_pending
                self.preview_pending = None
            self.preview_working = True
            proc: subprocess.Popen | None = None
            try:
                if seq != self.preview_seq:
                    continue
                self._emit('preview_stage', (seq, '正在启动播放器，等待音频输出…'))
                cmd = [self.tools[2], '-nodisp', '-autoexit', '-nostats', '-loglevel', 'error',
                       '-ss', f'{start:.1f}', '-t', f'{length:.1f}', '-i', str(source)]  # type: ignore[index]
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, creationflags=proc_flags(low=True))
                with self.preview_condition:
                    self.preview_proc = proc
                if seq != self.preview_seq:
                    proc.terminate()
                else:
                    self._emit('preview_started', (seq, length))
                deadline = time.monotonic() + length + 16
                while proc.poll() is None:
                    if self.preview_shutdown or seq != self.preview_seq:
                        proc.terminate()
                        break
                    if time.monotonic() >= deadline:
                        proc.kill()
                        raise TimeoutError('FFplay 超时，已结束进程')
                    time.sleep(.1)
                err = (proc.communicate(timeout=3)[1] or b'').decode('utf-8', 'replace').strip()
                self._emit('preview_finished', (seq, proc.returncode, err))
            except Exception as e:
                self._emit('preview_error', (seq, str(e)))
            finally:
                if proc and proc.poll() is None:
                    proc.kill()
                    try:
                        proc.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                with self.preview_condition:
                    if self.preview_proc is proc:
                        self.preview_proc = None
                self.preview_working = False

    def _apply(self, quiet: bool = False) -> bool:
        c = self._current()
        if not c:
            return False
        try:
            start = timestamp(self.start_var.get())
            length = norm_tenth(float(self.length_var.get().strip()))
            if not MIN_LENGTH <= length <= MAX_LENGTH:
                raise ValueError('时长应在 0.1～40 秒之间')
            name = output_name(self.name_var.get())
            if self.track_duration and start + length > self.track_duration + .05:
                raise ValueError('截取范围超过歌曲总时长')
        except (ValueError, OverflowError) as e:
            self.editor_var.set('设置不合法：' + str(e))
            return False
        changed = (start, length, name) != (c.start, c.length, c.output)
        c.start, c.length, c.output = start, length, name
        if changed:
            c.state, c.note = '待处理', ''
            self._dirty = True
            self.revision += 1
        self._update_row(c)
        self._draw_wave()
        self.editor_var.set('✓ 当前片段设置已记录在内存中' + (' · 尚未保存到 Excel' if self._dirty else ''))
        if not quiet:
            self._log('已保存当前片段设置（仅内存）：' + c.source.name)
        if not quiet:
            self.status_var.set(f'已保存当前片段配置：{c.source.name}；没有启动 FFmpeg、没有导出音频。')
        return True

    def _draw_wave(self) -> None:
        c = self.canvas
        w, h = max(100, c.winfo_width()), max(120, c.winfo_height())
        c.delete('all')
        c.create_line(8, h // 2, w - 8, h // 2, fill='#c5d2ec')
        length = self.track_duration
        if not length:
            c.create_text(w // 2, h // 2 - 28, text='正在读取歌曲时长 / 尚未选择歌曲', fill=GRAY)
            return
        if self.wave_peaks:
            n = len(self.wave_peaks)
            for x in range(5, w-5, 3):
                idx = min(n-1, int(x / w * n))
                amp = self.wave_peaks[idx] * (h-44)*.46
                c.create_line(x, h/2-amp, x, h/2+amp, fill='#91acd9', width=2)
        else:
            c.create_text(w//2, h//2-34, text='波形未加载（极速模式，仍可编辑和试听）', fill=GRAY)
        for i in range(5):
            x = 8 + i*(w-16)/4
            c.create_line(x, h-24, x, h-30, fill='#a7b6d0')
            c.create_text(x, h-12, text=time_label(i*length/4).split('.')[0], fill=GRAY, font=('Arial', 9))
        clip = self._current()
        if clip:
            x1 = 8+(w-16)*clip.start/length
            x2 = 8+(w-16)*(clip.start+clip.length)/length
            c.create_rectangle(max(8,x1), 7, min(w-8,x2), h-34, outline=ACCENT, width=2,
                               fill='#bcd7ff', stipple='gray50')
            if clip.start + clip.length > length+.05:
                c.create_text(w-8, 12, text='超出曲长', anchor='ne', fill=ERROR)

    def _drag_begin(self, ev: tk.Event) -> None:
        if not self._current() or not self.track_duration:
            return
        self.drag_anchor = self._canvas_time(ev.x)

    def _canvas_time(self, x: int) -> float:
        w = max(20, self.canvas.winfo_width())
        return norm_tenth(max(0.0, min(1.0, (x - 8) / max(1, w-16))) * self.track_duration)

    def _drag_move(self, ev: tk.Event) -> None:
        clip = self._current()
        if not clip or self.drag_anchor is None:
            return
        t = self._canvas_time(ev.x)
        if abs(t - self.drag_anchor) < MIN_LENGTH:
            return
        start = min(t, self.drag_anchor)
        duration = min(MAX_LENGTH, abs(t - self.drag_anchor))
        duration = norm_tenth(min(duration, self.track_duration - start))
        clip.start, clip.length = start, max(MIN_LENGTH, duration)
        self.start_var.set(time_label(clip.start))
        self.length_var.set(f'{clip.length:.1f}')
        self._dirty = True
        self.revision += 1
        self._draw_wave()

    def _drag_end(self, _ev: tk.Event) -> None:
        if self.drag_anchor is not None:
            self.drag_anchor = None
            self._apply(quiet=True)

    def _pick_files(self) -> None:
        if self.io_busy or self.saving or self.exporting:
            return
        names = filedialog.askopenfilenames(title='选择音频', filetypes=[('音乐文件', '*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus'), ('全部文件', '*')])
        if names:
            self._start_import('files', [Path(s) for s in names])

    def _pick_folder(self) -> None:
        if self.io_busy or self.saving or self.exporting:
            return
        name = filedialog.askdirectory(title='选择音乐文件夹')
        if name:
            self._start_import('folder', Path(name))

    def _start_import(self, mode: str, files: list[Path] | Path) -> None:
        self.io_busy = True
        self._io_buttons()
        self._job('import', '正在扫描文件名（不解码音频）')
        def run() -> None:
            try:
                if mode == 'folder':
                    folder = Path(files)
                    sources = sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() in SUPPORTED])
                else:
                    folder = None
                    sources = [p for p in files if p.is_file() and p.suffix.casefold() in SUPPORTED]
                clips = [Clip(p.name, p.resolve(), output=suggested_name(p)) for p in sources]
                self._emit('imported', (clips, folder))
            except Exception as e:
                self._emit('io_error', str(e))
        threading.Thread(target=run, daemon=True, name='mc-import').start()

    def _open_excel(self) -> None:
        if self.io_busy or self.saving or self.exporting:
            return
        path = filedialog.askopenfilename(title='选择 Excel 裁剪任务表', filetypes=[('Excel 工作簿', '*.xlsx')])
        if not path:
            return
        if self.clips and not messagebox.askyesno('替换任务', '打开 Excel 会替换现有任务列表，是否继续？'):
            return
        self.io_busy = True
        self._io_buttons()
        self._job('import', '正在读取 Excel 配置')
        def run() -> None:
            try:
                self._emit('excel_loaded', (load_excel(Path(path), self.audio_root), Path(path)))
            except Exception as e:
                self._emit('io_error', str(e))
        threading.Thread(target=run, daemon=True, name='mc-excel').start()

    def _save_excel(self) -> None:
        if self.io_busy or self.saving or self.exporting:
            return
        if self._current() and not self._apply(quiet=True):
            return
        if not self.clips:
            self.status_var.set('请先添加歌曲')
            return
        path = self.excel_path
        if path is None:
            choice = filedialog.asksaveasfilename(title='保存 Excel', defaultextension='.xlsx',
                initialfile='songs.xlsx', filetypes=[('Excel', '*.xlsx')])
            if not choice:
                return
            path = Path(choice)
        snapshot = [Clip(c.filename, c.source, c.start, c.length, c.output) for c in self.clips]
        saved_revision = self.revision
        folder = self.audio_root
        self.saving = True
        self._io_buttons()
        self._job('save', f'后台写入 Excel（{len(snapshot)} 行）')
        def run() -> None:
            try:
                save_excel(path, snapshot, folder)
                self._emit('save_done', (path, saved_revision))
            except Exception as e:
                self._emit('save_error', str(e))
        threading.Thread(target=run, daemon=True, name='mc-save').start()

    def _relink(self) -> None:
        if self.io_busy or self.saving or self.exporting:
            return
        folder = filedialog.askdirectory(title='重新指定音频文件夹')
        if not folder:
            return
        base = Path(folder)
        missing = [(c.uid, c.filename, c.source) for c in self.clips]
        self.io_busy = True
        self._io_buttons()
        self._job('import', '正在后台匹配音频文件（无需解码）')
        def run() -> None:
            try:
                linked = []
                for uid, name, source in missing:
                    if source.is_file():
                        continue
                    dest = base / re.split(r'[/\\]', name)[-1]
                    if dest.is_file():
                        linked.append((uid, dest.resolve()))
                self._emit('relink_done', (linked, base))
            except Exception as e:
                self._emit('io_error', str(e))
        threading.Thread(target=run, daemon=True, name='mc-relink').start()

    def _clone(self) -> None:
        c = self._current()
        if not c:
            return
        new = Clip(c.filename, c.source, c.start, c.length, c.output)
        seen = {c.output.casefold() for c in self.clips}
        basename = Path(new.output).stem
        n = 2
        while new.output.casefold() in seen:
            new.output = f'{basename}_{n}.mp3'
            n += 1
        index = self.clips.index(c)+1
        self.clips.insert(index, new)
        self.by_id[new.uid] = new
        self._dirty = True
        self.revision += 1
        self._rebuild_tree(select=new.uid)

    def _delete(self) -> None:
        c = self._current()
        if c is None:
            return
        if not messagebox.askyesno('移除片段', f'移除「{c.source.name}」的当前片段设置？\n不会删除原音频。'):
            return
        index = self.clips.index(c)
        self.clips.remove(c)
        self.by_id.pop(c.uid, None)
        self._dirty = True
        self.revision += 1
        uid = self.clips[min(index, len(self.clips)-1)].uid if self.clips else None
        self._rebuild_tree(select=uid)

    def _pick_output(self) -> None:
        name = filedialog.askdirectory(title='选择 MP3 片段导出目录', initialdir=str(self.output_dir))
        if name:
            self.output_dir = Path(name)
            self.output_var.set(str(self.output_dir))

    def _export(self) -> None:
        if self.io_busy or self.saving or self.exporting:
            return
        if not self.tools:
            self.status_var.set('FFmpeg 未配置')
            return
        if not self.clips:
            self.status_var.set('没有待导出的歌曲')
            return
        if self._current() and not self._apply(quiet=True):
            return
        self._stop_preview(quiet=True)
        self._cancel_wave(quiet=True)
        snapshots = [Clip(c.filename, c.source, c.start, c.length, c.output, uid=c.uid) for c in self.clips]
        dest, tools, overwrite = self.output_dir, self.tools, self.overwrite.get()
        self.cancel_export.clear()
        self.exporting = True
        self._io_buttons()
        self.cancel_button['state'] = 'normal'
        self.progress['value'] = 0
        self.progress_label.set(f'进度：0/{len(snapshots)}')
        self._job('export', f'准备批量导出 {len(snapshots)} 个片段')
        def run() -> None:
            try:
                result = batch_export(snapshots, dest, tools, overwrite, self.cancel_export,
                                      lambda k, x: self._emit(k, x))
                self._emit('export_done', result)
            except Exception as e:
                self._emit('export_error', str(e))
        threading.Thread(target=run, daemon=True, name='mc-export').start()

    def _cancel_export(self) -> None:
        self.cancel_export.set()
        self.status_var.set('已请求取消当前任务；正在终止子进程并生成已完成任务的报告。')
        self._job('export', '正在取消导出')

    def _on_close(self) -> None:
        if self.exporting and not messagebox.askyesno('仍在导出', '正在导出 MP3，退出会停止当前任务。确定退出？'):
            return
        self.cancel_export.set()
        self._stop_preview(quiet=True)
        self._cancel_wave(quiet=True)
        with self.wave_condition:
            self.wave_shutdown = True
            self.wave_condition.notify_all()
        with self.preview_condition:
            self.preview_shutdown = True
            self.preview_condition.notify_all()
        self.root.destroy()


def headless(args: argparse.Namespace) -> int:
    tools = get_tools(args.ffmpeg or '')
    book = Path(args.excel).expanduser().resolve()
    clips = load_excel(book, Path(args.input).resolve() if args.input else None)
    if not clips:
        print('Excel 里没有有效任务')
        return 2
    folder = Path(args.output).resolve() if args.output else book.parent / 'output'
    result = batch_export(clips, folder, tools, args.overwrite, threading.Event(),
                          lambda k, x: print(f'{k}: {x}' if k == 'export_row' else '', end='\n' if k == 'export_row' else ''))
    good, skipped, failed, report, canceled = result
    print(f'成功 {good}，跳过 {skipped}，失败 {failed}；报告：{report}')
    return int(failed > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description='MusicCutter v3.0 — 轻量化的可视化音乐裁剪工具')
    parser.add_argument('--batch', action='store_true', help='命令行批量模式，不启动界面')
    parser.add_argument('--excel', help='Excel 裁剪任务表')
    parser.add_argument('--input', help='源音乐目录')
    parser.add_argument('--output', help='输出目录')
    parser.add_argument('--ffmpeg', help='FFmpeg 路径')
    parser.add_argument('--overwrite', action='store_true', help='允许覆盖已有片段')
    args = parser.parse_args()
    if args.batch:
        if not args.excel:
            parser.error('--batch 需要 --excel 参数')
        try:
            return headless(args)
        except Exception as e:
            print('错误：', e, file=sys.stderr)
            return 2
    root = tk.Tk()
    MusicCutterApp(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
