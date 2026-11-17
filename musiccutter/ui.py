"""Tk view/controller. All file/codec work is delegated to serial worker lanes."""
from __future__ import annotations

from collections import OrderedDict
import math
import os
from pathlib import Path
import platform
import queue
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from . import APP_VERSION
from .diagnostics import configure_logging, data_directory
from .engine import check_tools, decode_wave, get_tools, probe_audio, process, run_batch
from .excel import load_excel, save_excel
from .files import scan_audio
from .models import (Clip, DEFAULT_LENGTH, MediaInfo, OUTPUT_FORMATS, SUPPORTED, name_key,
                     norm_tenth, output_name, suggested_name, time_label, timestamp,
                     unique_name, validate_range)
from .storage import Project, load_project, save_project
from .workers import Context, Event, Worker

from .view import MainView, BG, PAPER, INK, MUTED, ACCENT, ERROR, GREEN


class MusicCutterApp(MainView):
    def __init__(self, root: tk.Tk, *, data_dir: Path | None = None, restore: bool = True, ffmpeg: str = ''):
        self.root = root
        self.data_dir = data_dir or data_directory()
        self.log, self.recent, self.log_path = configure_logging(self.data_dir)
        self.recovery_path = self.data_dir / 'workspace.mcut.json'
        root.title(f'MusicCutter {APP_VERSION} · 音频剪辑')
        root.geometry('1240x820')
        root.minsize(1050, 740)
        root.configure(bg=BG)
        root.report_callback_exception = self._tk_exception
        self.events: queue.Queue[Event] = queue.Queue()
        self.workers = {name: Worker(name, self.events) for name in ('io', 'media', 'preview', 'export', 'save', 'tools')}
        self.clips: list[Clip] = []
        self.by_id: dict[int, Clip] = {}
        self.groups: dict[Path, str] = {}
        self.group_sources: dict[str, Path] = {}
        self.selected: int | None = None
        self.drafts: dict[int, dict[str, str]] = {}
        self.media_cache: OrderedDict[Path, tuple[tuple[int, int], MediaInfo]] = OrderedDict()
        self.wave_cache: OrderedDict[Path, tuple[tuple[int, int], list[float]]] = OrderedDict()
        self.tools = None
        self.custom_ffmpeg = ''
        self._startup_ffmpeg = ffmpeg
        self.tool_versions = ''
        self.project_path: Path | None = None
        self.output_dir = Path(__file__).resolve().parents[1] / 'output'
        self.last_report: Path | None = None
        self.track_duration = 0.0
        self.wave_peaks: list[float] = []
        self.revision = 0
        self.saved_revision = 0
        self.persisted_revision = -1
        self.save_inflight_revision = -1
        self.io_busy = False
        self.exporting = False
        self.saving_project = False
        self.recovery_ready = not restore
        self._loading_editor = False
        self._selecting = False
        self._closing = False
        self._destroyed = False
        self._commit_after = None
        self._save_after = None
        self._media_after = None
        self._draw_after = None
        self._save_retry_after = None
        self._failed_autosave = False
        self._save_operation = 'autosave'
        self._io_operation = ''
        self._jobs: dict[str, tuple[float, str]] = {}
        self._preview_started = 0.0
        self._preview_length = 0.0
        self._last_tick = time.monotonic()
        self._max_tick_delay = 0.0
        self._drag_anchor: float | None = None
        self._log_window = None
        self._log_signature = None
        self._insert_pending = False
        self._media_token = None
        self._preview_token = None
        self._removed: list[tuple[int, Clip, dict[str, str] | None]] = []

        self.start_var = tk.StringVar(value='00:00.0')
        self.length_var = tk.StringVar(value='15.0')
        self.name_var = tk.StringVar()
        self.format_var = tk.StringVar(value='MP3')
        self.recursive = tk.BooleanVar(value=True)
        self.overwrite = tk.BooleanVar(value=False)
        self.song_var = tk.StringVar(value='添加音频，开始剪辑')
        self.info_var = tk.StringVar(value='选择一个片段后，设置开始位置与时长。')
        self.end_var = tk.StringVar(value='结束位置  —')
        self.editor_var = tk.StringVar(value='支持秒数或 01:20.5；精度 0.1 秒。')
        self.wave_var = tk.StringVar(value='时间轴 · 波形按需加载')
        self.preview_var = tk.StringVar(value='选择范围后可试听')
        self.status_var = tk.StringVar(value='准备就绪')
        self.busy_var = tk.StringVar(value='后台任务：无')
        self.save_var = tk.StringVar(value='工作区自动保存')
        self.ffmpeg_var = tk.StringVar(value='正在检查音频引擎…')
        self.output_var = tk.StringVar(value=str(self.output_dir))
        self.count_var = tk.StringVar(value='0 个音频 · 0 个片段')
        self.progress_label = tk.StringVar(value='尚未导出')
        self._style()
        self._build_ui()
        for key, variable in (('start', self.start_var), ('length', self.length_var), ('output', self.name_var)):
            variable.trace_add('write', lambda *_args, key=key: self._editor_changed(key))
        self.root.after(30, self._drain_events)
        self.root.after(200, self._heartbeat)
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        self._refresh_controls()
        if restore:
            self._load_recovery()
        else:
            self._detect_tools()

    def _tk_exception(self, exc_type, exc, tb) -> None:
        self.log.error('界面回调异常', exc_info=(exc_type, exc, tb))
        self.status_var.set(f'操作发生异常：{exc}。可在「运行日志」查看详情。')

    def _set_step(self, current: int) -> None:
        for i, label in enumerate(self.step_labels):
            label.configure(bg='#DFEAFD' if i == current else '#E8EDF5', fg=ACCENT if i == current else MUTED)

    def _refresh_controls(self) -> None:
        locked = self.io_busy or self.exporting or self.saving_project or self._closing or not self.recovery_ready
        for button in (self.import_button, self.folder_button, self.open_button, self.save_button,
                       self.recursive_check, self.output_button, self.overwrite_check):
            button.configure(state='disabled' if locked else 'normal')
        current = self._current() is not None
        for button in (self.add_clip_button, self.clone_button, self.delete_button, self.relink_button):
            button.configure(state='normal' if current and not locked else 'disabled')
        for entry in (self.start_entry, self.length_entry, self.name_entry):
            entry.configure(state='disabled' if locked or not current else 'normal')
        self.format_combo.configure(state='readonly' if current and not locked else 'disabled')
        self.listen_button.configure(state='normal' if current and not locked and self.tools and self.tools[2] else 'disabled')
        self.wave_button.configure(state='normal' if current and not locked and self.tools else 'disabled')
        self.stop_button.configure(state='normal' if self.workers['preview'].busy else 'disabled')
        self.export_button.configure(state='normal' if self.clips and self.tools and not locked else 'disabled')
        self.retry_button.configure(state='normal' if any(c.state == '失败' for c in self.clips) and self.tools and not locked else 'disabled')
        self.cancel_button.configure(state='normal' if self.exporting and not self._closing else 'disabled')
        self.cancel_import_button.configure(state='normal' if self.io_busy and self._io_operation == 'import' and not self._insert_pending else 'disabled')
        self.result_button.configure(state='normal' if self.last_report else 'disabled')

    def _locked(self) -> bool:
        return self.io_busy or self.exporting or self.saving_project or self._closing or not self.recovery_ready

    def _current(self) -> Clip | None:
        return self.by_id.get(self.selected)

    def _job(self, lane: str, text: str | None) -> None:
        if text is None:
            self._jobs.pop(lane, None)
        else:
            self._jobs[lane] = (self._jobs.get(lane, (time.monotonic(), ''))[0], text)

    def _mark_changed(self) -> None:
        self.revision += 1
        self.save_var.set('工作区：等待自动保存…')
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self._save_after:
            self.root.after_cancel(self._save_after)
        if not self._closing:
            self._save_after = self.root.after(650, self._autosave)

    def _snapshot(self) -> Project:
        return Project([clip.snapshot() for clip in self.clips], self.output_dir,
                       {uid: dict(draft) for uid, draft in self.drafts.items()}, self.custom_ffmpeg, self.recursive.get())

    def _autosave(self) -> None:
        self._save_after = None
        if not self.recovery_ready or self.revision == self.persisted_revision:
            return
        if self.workers['save'].busy:
            self._save_after = self.root.after(200, self._autosave)
            return
        snapshot, revision = self._snapshot(), self.revision
        self.save_inflight_revision = revision
        self._save_operation = 'autosave'
        self.save_var.set('正在自动保存…')
        self.workers['save'].submit(lambda ctx: (save_project(self.recovery_path, snapshot), revision)[1])

    def _editor_changed(self, field: str = '') -> None:
        clip = self._current()
        if self._loading_editor or not clip or self._locked():
            return
        if field in ('start', 'length'):
            clip.auto_length = False
        self.drafts[clip.uid] = {'start': self.start_var.get(), 'length': self.length_var.get(), 'output': self.name_var.get()}
        clip.state, clip.note = '编辑中', ''
        self._update_row(clip)
        self._mark_changed()
        if self._commit_after:
            self.root.after_cancel(self._commit_after)
        self._commit_after = self.root.after(260, self._apply)
        try:
            end = timestamp(self.start_var.get()) + timestamp(self.length_var.get())
            self.end_var.set('结束位置  ' + time_label(end))
        except ValueError:
            self.end_var.set('结束位置  —')

    def _apply(self, quiet: bool = False) -> bool:
        if self._commit_after:
            self.root.after_cancel(self._commit_after)
        self._commit_after = None
        clip = self._current()
        if not clip:
            return False
        if self._loading_editor or self.exporting:
            return True
        try:
            start, length = validate_range(self.start_var.get(), self.length_var.get(), self.track_duration or None)
            name = output_name(self.name_var.get())
            if any(other.uid != clip.uid and name_key(other.output) == name_key(name) for other in self.clips):
                raise ValueError('输出名称与另一片段重复，请修改文件名')
        except ValueError as exc:
            if clip.uid not in self.drafts:
                self.drafts[clip.uid] = {'start': self.start_var.get(), 'length': self.length_var.get(), 'output': self.name_var.get()}
                self._mark_changed()
            clip.state, clip.note = '需修改', str(exc)
            self._update_row(clip)
            self.editor_var.set(str(exc) + '；输入已保留为草稿。')
            self.editor_label.configure(style='Error.TLabel')
            return False
        changed = (clip.start, clip.length, clip.output) != (start, length, name)
        had_draft = clip.uid in self.drafts
        clip.start, clip.length, clip.output = start, length, name
        if changed or had_draft:
            clip.state, clip.note = '待导出', ''
            self.drafts.pop(clip.uid, None)
            self._mark_changed()
            self._update_row(clip)
        self.end_var.set('结束位置  ' + time_label(clip.end))
        self.editor_var.set('设置已记录 · 工作区自动保存' if not clip.note else clip.note[:150])
        self.editor_label.configure(style='Good.TLabel' if clip.state != '失败' else 'Error.TLabel')
        self._draw_wave()
        return True

    def _format_changed(self, _event=None) -> None:
        if self._locked() or not self._current():
            return
        name = self.name_var.get().strip() or suggested_name(self._current().source)
        self.name_var.set(str(Path(name).with_suffix('.' + self.format_var.get().lower())))

    def _setting_changed(self) -> None:
        if not self._locked():
            self._mark_changed()

    def _insert_row(self, clip: Clip) -> None:
        group = self.groups.get(clip.source)
        if group is None:
            group = f'source-{len(self.groups) + 1}'
            while self.tree.exists(group):
                group += '-'
            self.groups[clip.source] = group
            self.group_sources[group] = clip.source
            self.tree.insert('', 'end', iid=group, text=clip.source.name, open=True, tags=('source',))
        self.tree.insert(group, 'end', iid=str(clip.uid))
        self._update_row(clip)

    def _update_row(self, clip: Clip) -> None:
        if not self.tree.exists(str(clip.uid)):
            return
        tags = ('error',) if clip.state in ('失败', '需修改', '无法读取') else ('success',) if clip.state == '成功' else ()
        self.tree.item(str(clip.uid), text=clip.output,
                       values=(f'{time_label(clip.start)} · {clip.length:.1f}s', clip.state), tags=tags)

    def _append_clips(self, clips: list[Clip], *, deduplicate: bool = False, rename: bool = True, on_done=None) -> None:
        self._insert_pending = True
        self._refresh_controls()
        existing = {clip.source for clip in self.clips}
        used = {name_key(clip.output) for clip in self.clips}
        pending = []
        for clip in clips:
            if deduplicate and clip.source in existing:
                continue
            existing.add(clip.source)
            if rename:
                clip.output = unique_name(clip.output or suggested_name(clip.source), used)
            pending.append(clip)
        cursor = 0
        first = pending[0].uid if pending else None

        def chunk() -> None:
            nonlocal cursor
            deadline = time.monotonic() + 0.005
            while cursor < len(pending) and time.monotonic() < deadline:
                clip = pending[cursor]
                self.clips.append(clip)
                self.by_id[clip.uid] = clip
                self._insert_row(clip)
                cursor += 1
            self.count_var.set(f'{len(self.groups)} 个音频 · {len(self.clips)} 个片段')
            if cursor < len(pending):
                self.root.after(1, chunk)
                return
            self._insert_pending = False
            if first is not None and self.selected is None:
                self._select(first)
            if on_done:
                on_done(len(pending))
            self._refresh_controls()
        chunk()

    def _select(self, uid: int) -> None:
        if self.tree.exists(str(uid)):
            self.tree.selection_set(str(uid))
            self.tree.focus(str(uid))
            self.tree.see(str(uid))
            self._on_select()

    def _on_select(self, _event=None) -> None:
        if self._selecting:
            return
        selection = self.tree.selection()
        if not selection:
            return
        item = selection[0]
        if item in self.group_sources:
            children = self.tree.get_children(item)
            if children:
                self._select(int(children[0]))
            return
        uid = int(item)
        if uid == self.selected:
            return
        if self._current() and not self.exporting:
            self._apply(quiet=True)
        self.selected = uid
        self._stop_preview(quiet=True)
        self.workers['media'].cancel()
        self._media_token = None
        self._job('media', None)
        self.wave_peaks, self.track_duration = [], 0.0
        self._load_editor()
        self._refresh_controls()
        self._set_step(1)
        if self._media_after:
            self.root.after_cancel(self._media_after)
        if not self.exporting:
            self._media_after = self.root.after(120, self._request_media)

    def _load_editor(self) -> None:
        clip = self._current()
        if not clip:
            return
        self._loading_editor = True
        draft = self.drafts.get(clip.uid)
        self.start_var.set(draft['start'] if draft else time_label(clip.start))
        self.length_var.set(draft['length'] if draft else f'{clip.length:.1f}')
        self.name_var.set(draft['output'] if draft else clip.output)
        self.format_var.set(Path(clip.output).suffix[1:].upper())
        self._loading_editor = False
        self.song_var.set(clip.source.name)
        self.info_var.set('正在读取音频信息…' if self.tools else '请在设置中配置 FFmpeg 后读取音频。')
        self.end_var.set('结束位置  ' + time_label(clip.end))
        self.editor_var.set('有未完成的编辑草稿，请检查后再导出。' if draft else (clip.note or '设置自动保存 · 支持 0.1 秒精度'))
        self.editor_label.configure(style='Error.TLabel' if draft or clip.state == '失败' else 'Muted.TLabel')
        self.wave_var.set('时间轴 · 波形按需加载')
        self._draw_wave()

    def _new_clip(self, duplicate: bool = False) -> None:
        clip = self._current()
        if self._locked() or not clip or not self._apply(quiet=True):
            return
        used = {name_key(c.output) for c in self.clips}
        length = clip.length if duplicate else min(DEFAULT_LENGTH, math.floor(self.track_duration * 10) / 10) if self.track_duration else DEFAULT_LENGTH
        new = Clip(clip.filename, clip.source, clip.start if duplicate else 0.0,
                   max(0.1, length), unique_name(clip.output if duplicate else suggested_name(clip.source, self.format_var.get().lower()), used),
                   state='待导出', auto_length=not duplicate and not bool(self.track_duration))
        self.clips.append(new)
        self.by_id[new.uid] = new
        self._insert_row(new)
        self.count_var.set(f'{len(self.groups)} 个音频 · {len(self.clips)} 个片段')
        self._mark_changed()
        self._select(new.uid)

    def _clone(self) -> None:
        self._new_clip(duplicate=True)

    def _delete(self) -> None:
        clip = self._current()
        if self._locked() or not clip:
            return
        index = self.clips.index(clip)
        self._removed.append((index, clip.snapshot(), self.drafts.get(clip.uid)))
        self._removed = self._removed[-10:]
        self.workers['media'].cancel()
        self._stop_preview(quiet=True)
        self._media_token = None
        self._job('media', None)
        self._selecting = True
        self.tree.delete(str(clip.uid))
        group = self.groups[clip.source]
        if not self.tree.get_children(group):
            self.tree.delete(group)
            self.groups.pop(clip.source)
            self.group_sources.pop(group)
        self.clips.remove(clip)
        self.by_id.pop(clip.uid)
        self.drafts.pop(clip.uid, None)
        self.selected = None
        self._selecting = False
        self._mark_changed()
        if self.clips:
            self._select(self.clips[min(index, len(self.clips) - 1)].uid)
        else:
            self._empty_editor()
        self.count_var.set(f'{len(self.groups)} 个音频 · {len(self.clips)} 个片段')
        self._refresh_controls()
        self.status_var.set(f'已移除「{clip.output}」。可从「编辑」菜单撤销移除。')

    def _undo_remove(self) -> None:
        if self._locked() or not self._removed:
            return
        index, clip, draft = self._removed.pop()
        self.clips.insert(min(index, len(self.clips)), clip)
        self.by_id[clip.uid] = clip
        if draft:
            self.drafts[clip.uid] = draft
        self._insert_row(clip)
        self.count_var.set(f'{len(self.groups)} 个音频 · {len(self.clips)} 个片段')
        self._mark_changed()
        self._select(clip.uid)
        self._refresh_controls()
        self.status_var.set(f'已恢复「{clip.output}」。')

    def _empty_editor(self) -> None:
        self._loading_editor = True
        self.start_var.set('00:00.0')
        self.length_var.set('15.0')
        self.name_var.set('')
        self._loading_editor = False
        self.track_duration, self.wave_peaks = 0.0, []
        self.song_var.set('添加音频，开始剪辑')
        self.info_var.set('一首音频可以创建多个独立片段。')
        self.editor_var.set('设置自动保存 · 支持 0.1 秒精度')
        self.end_var.set('结束位置  —')
        self._draw_wave()
        self._set_step(0)

    def _detect_tools(self, custom: str | None = None) -> None:
        if self.exporting or self._closing:
            return
        if custom is not None:
            self.custom_ffmpeg = custom
            self._startup_ffmpeg = ''
        elif self._startup_ffmpeg:
            self.custom_ffmpeg = self._startup_ffmpeg
        configured = self.custom_ffmpeg
        self.tools = None
        self.ffmpeg_var.set('正在检查 FFmpeg、FFprobe 与编码器…')
        self._job('tools', '检查音频引擎')
        def work(ctx: Context):
            tools = get_tools(configured)
            return tools, check_tools(tools, ctx.cancel.is_set)
        self.workers['tools'].submit(work)
        self._refresh_controls()

    def _request_media(self, waveform: bool = False) -> None:
        self._media_after = None
        clip = self._current()
        if not clip or not self.tools or self.exporting or self._closing:
            return
        path, tools = clip.source, self.tools
        self._media_mode = 'wave' if waveform else 'info'
        self._job('media', '生成波形（可取消）' if waveform else '读取所选音频信息')
        self.wave_var.set('正在生成波形…' if waveform else '正在读取时长…')
        self.wave_button.configure(text='取消波形' if waveform else '加载波形')

        def work(ctx: Context):
            stat = path.stat()
            fingerprint = (stat.st_size, stat.st_mtime_ns)
            cached = self.media_cache.get(path)
            info = cached[1] if cached and cached[0] == fingerprint else probe_audio(tools[1], path, ctx.cancel.is_set)
            ctx.check()
            self.media_cache[path] = (fingerprint, info)
            self.media_cache.move_to_end(path)
            while len(self.media_cache) > 256:
                self.media_cache.popitem(last=False)
            ctx.emit('media_info', (path, info))
            cached_wave = self.wave_cache.get(path)
            peaks = cached_wave[1] if cached_wave and cached_wave[0] == fingerprint else []
            if waveform and not peaks:
                peaks = decode_wave(tools[0], path, ctx.cancel.is_set)
                ctx.check()
                self.wave_cache[path] = (fingerprint, peaks)
                self.wave_cache.move_to_end(path)
                while len(self.wave_cache) > 5:
                    self.wave_cache.popitem(last=False)
            return path, peaks
        self._media_token = self.workers['media'].submit(work)

    def _show_wave(self) -> None:
        if self.workers['media'].busy and getattr(self, '_media_mode', '') == 'wave':
            self.workers['media'].cancel()
            self.wave_var.set('正在停止波形解析…')
        else:
            self._request_media(waveform=True)

    def _start_preview(self) -> None:
        clip = self._current()
        if self._locked() or not clip or not self._apply(quiet=True):
            return
        if not self.tools or not self.tools[2]:
            self.preview_var.set('缺少 FFplay，可在设置中更换音频引擎。导出功能不受影响。')
            return
        self.workers['media'].cancel()
        player, source, start, length = self.tools[2], clip.source, clip.start, clip.length
        self._preview_started = 0.0
        self._preview_length = length
        self.preview_var.set('正在启动试听…')
        self._job('preview', '试听片段')
        def work(ctx: Context):
            rc, _, err = process([player, '-nodisp', '-autoexit', '-nostats', '-loglevel', 'error',
                '-ss', f'{start:.1f}', '-t', f'{length:.1f}', '-i', str(source)],
                timeout=length + 20, stop=ctx.cancel.is_set, low=True,
                on_started=lambda: ctx.emit('playing'))
            if rc:
                raise RuntimeError(err.decode('utf-8', 'replace').strip() or f'播放器退出码 {rc}')
        self._preview_token = self.workers['preview'].submit(work)
        self._refresh_controls()

    def _stop_preview(self, quiet: bool = False) -> None:
        self.workers['preview'].cancel()
        self._preview_started = 0.0
        if not quiet:
            self.preview_var.set('正在停止试听…' if self.workers['preview'].busy else '试听已停止')
        else:
            self._preview_token = None
            self._job('preview', None)

    def _draw_wave(self) -> None:
        self._draw_after = None
        canvas = self.canvas
        canvas.delete('all')
        width, height = max(100, canvas.winfo_width()), max(52, canvas.winfo_height())
        middle = (height - 23) / 2
        duration = self.track_duration
        canvas.create_line(10, middle, width - 10, middle, fill='#D4DFEE')
        if self.wave_peaks:
            count = min(240, max(20, (width - 20) // 2))
            for i in range(count):
                peak = self.wave_peaks[min(len(self.wave_peaks) - 1, i * len(self.wave_peaks) // count)]
                x = 10 + i * (width - 20) / max(1, count - 1)
                amplitude = max(1, peak * (middle - 5))
                canvas.create_line(x, middle - amplitude, x, middle + amplitude, fill='#8BA9D5', width=1)
        if duration:
            clip = self._current()
            try:
                start, length = timestamp(self.start_var.get()), timestamp(self.length_var.get())
            except ValueError:
                start, length = (clip.start, clip.length) if clip else (0.0, 0.0)
            x1 = 10 + min(1.0, start / duration) * (width - 20)
            x2 = 10 + min(1.0, (start + length) / duration) * (width - 20)
            canvas.create_rectangle(x1, 3, max(x1 + 2, x2), height - 23, outline=ACCENT, width=2)
            for seconds, anchor, x in ((0, 'w', 10), (duration / 2, 'center', width / 2), (duration, 'e', width - 10)):
                canvas.create_text(x, height - 11, text=time_label(seconds), anchor=anchor, fill=MUTED, font=('Arial', 9))
        else:
            canvas.create_text(width / 2, middle, text='读取音频时长后可拖动选择范围', fill=MUTED)

    def _canvas_time(self, x: int) -> float:
        width = max(100, self.canvas.winfo_width())
        return math.floor(max(0.0, min(1.0, (x - 10) / (width - 20))) * self.track_duration * 10) / 10

    def _drag_begin(self, event) -> None:
        if not self._locked() and self.track_duration and self._current():
            self._drag_anchor = self._canvas_time(event.x)

    def _drag_move(self, event) -> None:
        if self._drag_anchor is None or self._locked():
            return
        position = self._canvas_time(event.x)
        start = min(position, self._drag_anchor)
        length = norm_tenth(abs(position - self._drag_anchor))
        if length >= 0.1:
            self._loading_editor = True
            self.start_var.set(time_label(start))
            self.length_var.set(f'{length:.1f}')
            self._loading_editor = False
            self._editor_changed('start')
            if self._draw_after is None:
                self._draw_after = self.root.after(25, self._draw_wave)

    def _drag_end(self, _event) -> None:
        if self._drag_anchor is not None:
            self._drag_anchor = None
            self._apply(quiet=True)

    def _pick_files(self) -> None:
        if self._locked():
            return
        choices = filedialog.askopenfilenames(parent=self.root, title='选择一个或多个音频文件',
            filetypes=[('音频', ' '.join('*' + suffix for suffix in sorted(SUPPORTED))), ('全部文件', '*')])
        if choices:
            self._start_import('files', [Path(path) for path in choices])

    def _pick_folder(self) -> None:
        if self._locked():
            return
        folder = filedialog.askdirectory(parent=self.root, title='选择音频文件夹')
        if folder:
            self._start_import('folder', Path(folder))

    def _start_import(self, mode: str, files) -> None:
        if self._locked():
            return
        if self._current():
            self._apply(quiet=True)
        self.io_busy, self._io_operation = True, 'import'
        recursive = self.recursive.get()
        paths = [files] if mode == 'folder' else list(files)
        self._set_step(0)
        self._job('io', '扫描音频文件')
        self.status_var.set('正在扫描文件名；不批量解码音频。')
        self._refresh_controls()
        def work(ctx: Context):
            return scan_audio(paths, recursive, ctx.cancel.is_set,
                              lambda found, seen: ctx.emit('scan', (found, seen)))
        self.workers['io'].submit(work)

    def _cancel_import(self) -> None:
        if self.io_busy and self._io_operation == 'import':
            self.workers['io'].cancel()
            self.status_var.set('正在停止扫描，将保留已找到的音频。')

    def _load_recovery(self) -> None:
        self.io_busy, self._io_operation = True, 'restore'
        self._job('io', '读取上次工作区')
        self._refresh_controls()
        def work(ctx: Context):
            if not self.recovery_path.exists():
                return None
            try:
                return load_project(self.recovery_path)
            except Exception:
                # Preserve an unreadable recovery file before a new workspace can be saved.
                backup = self.recovery_path.with_name(f'workspace.unreadable-{time.time_ns()}.json')
                self.recovery_path.replace(backup)
                self.log.error('损坏的工作区已保留为 %s', backup)
                raise
        self.workers['io'].submit(work)

    def _replace_project(self, project: Project, done=None) -> None:
        self.workers['media'].cancel()
        self._job('media', None)
        self._stop_preview(quiet=True)
        self._selecting = True
        self.tree.delete(*self.tree.get_children())
        self.clips, self.by_id, self.groups, self.group_sources = [], {}, {}, {}
        self.selected = None
        self._media_token = None
        self._removed = []
        self.drafts = project.drafts
        self.custom_ffmpeg = project.ffmpeg
        self.recursive.set(project.recursive)
        if project.output_dir:
            self.output_dir = project.output_dir
            self.output_var.set(str(self.output_dir))
        self._selecting = False
        self._empty_editor()
        def inserted(count):
            if done:
                done(count)
            self._refresh_controls()
        self._append_clips(project.clips, rename=False, on_done=inserted)

    def _open_project(self) -> None:
        if self._locked():
            return
        path = filedialog.askopenfilename(parent=self.root, title='打开 MusicCutter 项目',
                                          filetypes=[('MusicCutter 项目', '*.mcut.json'), ('JSON', '*.json')])
        if not path or not self._confirm_replace():
            return
        self.io_busy, self._io_operation = True, 'project'
        self._pending_project_path = Path(path)
        self._job('io', '读取项目')
        self._refresh_controls()
        self.workers['io'].submit(lambda ctx: load_project(Path(path)))

    def _confirm_replace(self) -> bool:
        if self._current():
            self._apply(quiet=True)
        return not self.clips or messagebox.askyesno('替换工作区', '打开后将替换当前片段列表。需要保留当前项目时，请先使用「保存项目」。继续？', parent=self.root)

    def _save_project_dialog(self) -> None:
        if self._locked() or not self.clips:
            return
        if self._current():
            self._apply(quiet=True)  # invalid drafts are saved too
        choice = filedialog.asksaveasfilename(parent=self.root, title='保存 MusicCutter 项目',
            defaultextension='.mcut.json', initialfile=self.project_path.name if self.project_path else '音频剪辑.mcut.json',
            filetypes=[('MusicCutter 项目', '*.mcut.json')])
        if not choice:
            return
        snapshot, revision = self._snapshot(), self.revision
        self.saving_project = True
        self._io_operation = 'save_project'
        self._job('io', '保存项目')
        self._refresh_controls()
        path = Path(choice)
        def work(ctx):
            save_project(path, snapshot)
            return path, revision
        self.workers['io'].submit(work)

    def _open_excel(self) -> None:
        if self._locked():
            return
        choice = filedialog.askopenfilename(parent=self.root, title='导入旧版 Excel 配置', filetypes=[('Excel', '*.xlsx')])
        if not choice or not self._confirm_replace():
            return
        self.io_busy, self._io_operation = True, 'excel'
        self._job('io', '读取 Excel 配置')
        self._refresh_controls()
        self.workers['io'].submit(lambda ctx: Project(load_excel(Path(choice))))

    def _save_excel(self) -> None:
        if self._locked() or not self.clips:
            return
        if self._current() and not self._apply(quiet=True) or self.drafts:
            self.status_var.set('请先修正编辑草稿，再导出 Excel；保存项目仍可保留草稿。')
            return
        choice = filedialog.asksaveasfilename(parent=self.root, title='导出 Excel 配置', defaultextension='.xlsx',
                                             initialfile='裁剪任务.xlsx', filetypes=[('Excel', '*.xlsx')])
        if not choice:
            return
        snapshot = [clip.snapshot() for clip in self.clips]
        self.io_busy, self._io_operation = True, 'save_excel'
        self._job('io', '写入 Excel 配置')
        self._refresh_controls()
        self.workers['io'].submit(lambda ctx: (save_excel(Path(choice), snapshot), choice)[1])

    def _clear_project(self) -> None:
        if self._locked() or not self.clips or not messagebox.askyesno('清空工作区', '移除全部片段设置？如需保留，请先保存项目。原音频不受影响。', parent=self.root):
            return
        self._replace_project(Project(output_dir=self.output_dir, ffmpeg=self.custom_ffmpeg))
        self.project_path = None
        self._mark_changed()
        self.status_var.set('工作区已清空')

    def _relink(self) -> None:
        clip = self._current()
        if self._locked() or not clip:
            return
        choice = filedialog.askopenfilename(parent=self.root, title=f'重新定位 {clip.source.name}',
                                            filetypes=[('音频', ' '.join('*' + ext for ext in sorted(SUPPORTED))), ('全部文件', '*')])
        if not choice:
            return
        old = clip.source
        self.io_busy, self._io_operation = True, 'relink'
        self._job('io', '检查重新定位的音频')
        self._refresh_controls()
        def work(ctx):
            path = Path(choice).resolve(strict=True)
            if not path.is_file():
                raise ValueError('所选路径不是文件')
            return old, path
        self.workers['io'].submit(work)

    def _apply_range_all(self) -> None:
        if self._locked() or not self._current() or not self._apply(quiet=True):
            return
        clip = self._current()
        if not messagebox.askyesno('应用时间范围', f'将开始 {time_label(clip.start)}、时长 {clip.length:.1f} 秒应用到全部 {len(self.clips)} 个片段？超出各自音频长度的片段将在检查时提示。', parent=self.root):
            return
        for other in self.clips:
            other.start, other.length, other.auto_length = clip.start, clip.length, False
            other.state, other.note = '待检查', ''
            draft = self.drafts.get(other.uid)
            if draft:
                draft.update(start=time_label(clip.start), length=f'{clip.length:.1f}')
            self._update_row(other)
        self._mark_changed()
        self.status_var.set(f'已将时间范围应用到 {len(self.clips)} 个片段；导出前将检查各自长度。')

    def _pick_output(self) -> None:
        if self._locked():
            return
        choice = filedialog.askdirectory(parent=self.root, title='选择片段输出目录')
        if choice:
            self.output_dir = Path(choice)
            self.output_var.set(str(self.output_dir))
            self._mark_changed()

    def _export(self, failed_only: bool = False) -> None:
        if self._locked() or not self.tools or not self.clips:
            return
        if self._current() and not self._apply(quiet=True):
            return
        if self.drafts:
            uid = next(iter(self.drafts))
            self._select(uid)
            self.status_var.set('存在尚未修正的编辑草稿，已定位；请修正后再导出。')
            return
        snapshots = [clip.snapshot() for clip in self.clips if not failed_only or clip.state == '失败']
        if not snapshots:
            return
        self._stop_preview(quiet=True)
        self.workers['media'].cancel()
        self.exporting = True
        self.progress.configure(value=0)
        self.progress_label.set(f'0 / {len(snapshots)}')
        self._set_step(2)
        self._job('export', f'准备 {len(snapshots)} 个片段')
        self.status_var.set('导出中：检查 → 编码 → 验证 → 写入。可以随时取消。')
        folder, tools, overwrite = self.output_dir, self.tools, self.overwrite.get()
        protected = {clip.source for clip in self.clips}
        self._refresh_controls()
        self.log.info('开始导出 tasks=%s output=%s overwrite=%s', len(snapshots), folder, overwrite)
        self.workers['export'].submit(lambda ctx: run_batch(snapshots, folder, tools, overwrite, ctx.cancel,
                                                           lambda kind, value: ctx.emit(kind, value),
                                                           protected_sources=protected))

    def _cancel_export(self) -> None:
        if self.exporting:
            self.workers['export'].cancel()
            self.cancel_button.configure(state='disabled')
            self.status_var.set('正在取消：终止当前进程、清理临时片段并生成完整报告…')
            self._job('export', '正在取消并收尾')

    def _drain_events(self) -> None:
        if self._destroyed:
            return
        deadline = time.monotonic() + 0.006
        while time.monotonic() < deadline:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.token != self.workers[event.lane].token:
                continue
            if event.kind != 'idle' and event.lane in ('media', 'preview'):
                expected = self._media_token if event.lane == 'media' else self._preview_token
                if event.token != expected:
                    continue
            self._event(event)
        self.root.after(25, self._drain_events)

    def _event(self, event: Event) -> None:
        lane, kind, value = event.lane, event.kind, event.value
        if kind == 'idle':
            self._refresh_controls()
            return
        if kind == 'started':
            return
        if kind == 'scan':
            found, inspected = value
            self.status_var.set(f'正在扫描：检查 {inspected} 个文件，找到 {found} 个音频…')
            return
        if kind == 'media_info':
            path, info = value
            clip = self._current()
            if not clip or clip.source != path or self.exporting:
                return
            self.track_duration = info.duration
            self.info_var.set(f'{time_label(info.duration)}  ·  {info.codec.upper()}  ·  {info.sample_rate / 1000:g} kHz  ·  {info.channels} 声道')
            changed = False
            for other in self.clips:
                if other.source == path and other.auto_length and other.uid not in self.drafts:
                    other.length = min(DEFAULT_LENGTH, math.floor((info.duration + 1e-6) * 10) / 10)
                    other.auto_length = False
                    if other.length < 0.1:
                        other.length = 0.1
                    self._update_row(other)
                    changed = True
            if changed:
                if clip.uid not in self.drafts:
                    self._loading_editor = True
                    self.length_var.set(f'{clip.length:.1f}')
                    self._loading_editor = False
                self._mark_changed()
            self._apply(quiet=True)
            self._draw_wave()
            return
        if kind == 'playing':
            self._preview_started = time.monotonic()
            self.preview_var.set('正在试听（进度为计时估算）')
            self.log.info('试听已启动')
            return
        if kind == 'export_range':
            uid, start, length = value
            clip = self.by_id.get(uid)
            if clip:
                clip.start, clip.length, clip.auto_length = start, length, False
                self._mark_changed()
                if uid == self.selected:
                    self._load_editor()
            return
        if kind == 'export_stage':
            index, count, uid, stage = value
            clip = self.by_id.get(uid)
            self._job('export', f'{index}/{count} · {stage} · {clip.output[:38] if clip else ""}')
            if clip:
                clip.state = stage + '中'
                self._update_row(clip)
            return
        if kind == 'export_progress':
            index, count, _uid, fraction = value
            self.progress.configure(value=(index - 1 + fraction) * 100 / max(1, count))
            self.progress_label.set(f'{index} / {count} · {fraction * 100:.0f}%')
            return
        if kind == 'export_row':
            index, count, uid, status, detail = value
            clip = self.by_id.get(uid)
            if clip:
                clip.state, clip.note = status, detail
                self._update_row(clip)
                if uid == self.selected:
                    self.editor_var.set(detail[:160])
                    self.editor_label.configure(style='Error.TLabel' if status == '失败' else 'Muted.TLabel')
            self.progress.configure(value=index * 100 / max(1, count))
            self.progress_label.set(f'{index} / {count}')
            self.log.info('片段结果 %s/%s uid=%s %s %s', index, count, uid, status, detail)
            return
        if kind in ('error', 'cancelled'):
            self._job(lane, None)
            if lane == 'media':
                self.wave_button.configure(text='加载波形')
                self.wave_var.set('解析已取消' if kind == 'cancelled' else '读取失败 · 查看日志')
                if kind == 'error':
                    self.info_var.set(str(value)[:180])
                    clip = self._current()
                    if clip:
                        clip.state, clip.note = '无法读取', str(value)
                        self._update_row(clip)
            elif lane == 'preview':
                self._preview_started = 0.0
                self.preview_var.set('试听已停止' if kind == 'cancelled' else '试听失败：' + str(value)[:130])
            elif lane == 'tools':
                self.tools = None
                self.ffmpeg_var.set('音频引擎不可用 · 点击「设置」修复')
                self.status_var.set(str(value)[:200] if kind == 'error' else '音频引擎检查已取消')
            elif lane == 'save':
                self._failed_autosave = True
                self.save_var.set('自动保存失败 · 查看日志')
                self.status_var.set('自动保存失败：' + str(value)[:180] + '。请检查磁盘和目录权限。')
            elif lane == 'export':
                self.exporting = False
                self.status_var.set('导出失败：' + str(value)[:200] if kind == 'error' else '导出已取消')
            elif lane == 'io':
                self.io_busy = self.saving_project = False
                if self._io_operation == 'restore':
                    self.recovery_ready = True
                    self.status_var.set('上次工作区无法恢复，原文件已保留。可在日志中查看原因。')
                    self._detect_tools()
                else:
                    self.status_var.set('操作已取消' if kind == 'cancelled' else '文件操作失败：' + str(value)[:180])
            self._refresh_controls()
            return
        if kind != 'done':
            return
        self._job(lane, None)
        if lane == 'tools':
            self.tools, self.tool_versions = value
            self.ffmpeg_var.set('音频引擎就绪  ·  MP3 / FLAC / WAV 导出  ·  ' + ('试听可用' if self.tools[2] else '未找到 FFplay，试听不可用'))
            self.log.info('音频引擎 %s\n%s', self.tools, self.tool_versions)
            self._request_media()
        elif lane == 'media':
            path, peaks = value
            if self._current() and self._current().source == path:
                self.wave_peaks = peaks
                self.wave_var.set('拖动时间轴选择范围' if peaks else '时间轴 · 可拖动选择范围')
                self.wave_button.configure(text='加载波形')
                self._draw_wave()
        elif lane == 'preview':
            self._preview_started = 0.0
            self.preview_var.set('试听结束')
            self.log.info('试听正常结束')
        elif lane == 'save':
            self.persisted_revision = value
            self._failed_autosave = False
            self.save_var.set('工作区已自动保存' if self.revision == value else '有新的修改，等待自动保存…')
            if self.revision != value:
                self._schedule_save()
        elif lane == 'export':
            result = value
            self.exporting = False
            self.last_report, self.last_result = result.report, result
            self.status_var.set(f'导出{"已取消" if result.was_cancelled else "完成"}：成功 {result.success}，跳过 {result.skipped}，失败 {result.failed}，取消 {result.cancelled}，未开始 {result.pending}。点击「查看结果」。')
            self.log.info('导出结束 %s', result)
            self._request_media()
        elif lane == 'io':
            operation = self._io_operation
            if operation == 'import':
                scanned = value
                for issue in scanned.issues:
                    self.log.warning('导入问题 %s', issue)
                candidates = [Clip(path.name, path, output=suggested_name(path), auto_length=True) for path in scanned.paths]
                def finished(count):
                    self.io_busy = False
                    duplicates = len(candidates) - count + scanned.duplicates
                    self.status_var.set(f'扫描{"已停止" if scanned.cancelled else "完成"}：添加 {count} 个音频，重复 {duplicates}，忽略 {scanned.ignored}，问题 {len(scanned.issues)}。每首可新增多个片段。')
                    self.log.info(self.status_var.get())
                    if count:
                        self._mark_changed()
                        self._set_step(1)
                    self._refresh_controls()
                self._append_clips(candidates, deduplicate=True, on_done=finished)
            elif operation in ('restore', 'project', 'excel'):
                self.recovery_ready = True
                if value is None:
                    self.io_busy = False
                    self.persisted_revision = self.revision
                    self._detect_tools()
                else:
                    def finished(count):
                        self.io_busy = False
                        if operation == 'restore':
                            self.persisted_revision = self.revision
                            self.status_var.set(f'已恢复上次工作区：{count} 个片段，含 {len(self.drafts)} 个未完成草稿。')
                        else:
                            self.project_path = self._pending_project_path if operation == 'project' else None
                            self._mark_changed()
                            self.status_var.set(f'已打开 {count} 个片段。缺失音频可使用「定位源文件」。')
                        self.log.info(self.status_var.get())
                        self._detect_tools()
                        self._refresh_controls()
                    self._replace_project(value, finished)
            elif operation == 'save_project':
                self.saving_project = False
                self.project_path, self.saved_revision = value
                self.status_var.set('项目已保存：' + str(self.project_path))
                self.log.info(self.status_var.get())
            elif operation == 'save_excel':
                self.io_busy = False
                self.status_var.set('Excel 配置已保存：' + str(value))
            elif operation == 'relink':
                self.io_busy = False
                old, new = value
                for clip in self.clips:
                    if clip.source == old:
                        clip.source, clip.filename, clip.state, clip.note = new, new.name, '待检查', ''
                # Rebuild grouping; preserve individual names and ranges.
                snapshot = self._snapshot()
                def linked(_count):
                    self._mark_changed()
                    self._request_media()
                self._replace_project(snapshot, linked)
        self._refresh_controls()

    def _heartbeat(self) -> None:
        if self._destroyed:
            return
        now = time.monotonic()
        delay = now - self._last_tick
        self._max_tick_delay = max(self._max_tick_delay, delay)
        if delay > 1.2:
            self.log.warning('界面心跳间隔 %.3fs', delay)
        self._last_tick = now
        jobs = [f'{text} · {now - started:.0f}s' for started, text in self._jobs.values()]
        self.busy_var.set('  |  '.join(jobs) if jobs else '准备就绪 · 所有任务已结束')
        if self._preview_started:
            elapsed = min(self._preview_length, now - self._preview_started)
            self.preview_var.set(f'试听 {elapsed:.1f} / {self._preview_length:.1f} 秒（计时估算）')
        if self._log_window and self._log_window.winfo_exists():
            self._refresh_log_view()
        self.root.after(200, self._heartbeat)

    def _settings(self) -> None:
        if self.exporting or self._closing:
            return
        window = tk.Toplevel(self.root)
        window.title('音频引擎设置')
        window.geometry('650x330')
        window.transient(self.root)
        frame = ttk.Frame(window, padding=22)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='音频引擎', font=('Arial', 16, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='FFmpeg 与 FFprobe 负责读取和导出；FFplay 用于试听。\n可放在程序的 bin 目录，或在下方指定 FFmpeg。', wraplength=590).pack(anchor='w', pady=12)
        path_var = tk.StringVar(value=self.tools[0] if self.tools else self.custom_ffmpeg)
        ttk.Entry(frame, textvariable=path_var, state='readonly').pack(fill='x')
        def pick():
            choice = filedialog.askopenfilename(parent=window, title='选择 FFmpeg 可执行文件')
            if choice:
                self._detect_tools(choice)
                self._mark_changed()
                window.destroy()
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=12)
        ttk.Button(row, text='选择 FFmpeg…', command=pick).pack(side='left')
        def automatic():
            self._detect_tools('')
            self._mark_changed()
            window.destroy()
        ttk.Button(row, text='重新自动检测', command=automatic).pack(side='left', padx=8)
        ttk.Label(frame, textvariable=self.ffmpeg_var, wraplength=590).pack(anchor='w')
        ttk.Label(frame, text='本地处理，无音频上传。MP3 192 kbps；FLAC 无损编码；WAV 16-bit PCM。', style='Small.TLabel', wraplength=590).pack(anchor='w', pady=12)

    def _show_logs(self) -> None:
        if self._log_window and self._log_window.winfo_exists():
            self._log_window.lift()
            return
        window = self._log_window = tk.Toplevel(self.root)
        window.title('MusicCutter · 运行日志')
        window.geometry('980x550')
        window.transient(self.root)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='本次运行日志（完整历史按文件轮转保存）').pack(anchor='w')
        ttk.Label(frame, text=str(self.log_path), style='Small.TLabel', wraplength=930).pack(anchor='w', pady=(5, 10))
        holder = ttk.Frame(frame)
        holder.pack(fill='both', expand=True)
        self.log_view = tk.Text(holder, wrap='word', state='disabled', font=('Menlo' if sys.platform == 'darwin' else 'Consolas', 10), bg=PAPER, relief='flat')
        scroll = ttk.Scrollbar(holder, command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.log_view.pack(side='left', fill='both', expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(10, 0))
        ttk.Button(buttons, text='复制诊断', command=self._copy_diagnostics).pack(side='left')
        ttk.Button(buttons, text='打开日志文件夹', command=lambda: self._open_path(self.log_path.parent)).pack(side='left', padx=8)
        ttk.Button(buttons, text='打开完整日志', command=lambda: self._open_path(self.log_path)).pack(side='left')
        self._log_signature = None
        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        entries = list(self.recent.entries)
        signature = (len(entries), entries[-1] if entries else '')
        if signature == self._log_signature:
            return
        self._log_signature = signature
        self.log_view.configure(state='normal')
        self.log_view.delete('1.0', 'end')
        self.log_view.insert('end', '\n'.join(entries))
        self.log_view.see('end')
        self.log_view.configure(state='disabled')

    def _copy_diagnostics(self) -> None:
        body = '\n'.join([f'MusicCutter {APP_VERSION}', f'系统：{platform.platform()}',
            f'Python：{sys.version.split()[0]}', f'音频引擎：{self.tools}', self.tool_versions,
            f'音频 {len(self.groups)} / 片段 {len(self.clips)} / 草稿 {len(self.drafts)}',
            f'工作区版本 {self.revision} / 自动保存版本 {self.persisted_revision}',
            f'后台：{self.busy_var.get()}', f'状态：{self.status_var.get()}',
            f'日志：{self.log_path}', '', *list(self.recent.entries)])
        self.root.clipboard_clear()
        self.root.clipboard_append(body)
        self.status_var.set('诊断信息已复制；包含本地文件路径和错误详情。')

    def _show_result(self) -> None:
        if not self.last_report:
            return
        result = self.last_result
        window = tk.Toplevel(self.root)
        window.title('导出结果')
        window.geometry('620x300')
        window.transient(self.root)
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='导出已取消' if result.was_cancelled else '导出完成', font=('Arial', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=f'成功 {result.success}   跳过 {result.skipped}   失败 {result.failed}\n已取消 {result.cancelled}   未开始 {result.pending}').pack(anchor='w', pady=15)
        ttk.Label(frame, text='逐项报告：' + str(result.report), wraplength=560).pack(anchor='w')
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=18)
        ttk.Button(actions, text='打开输出文件夹', command=lambda: self._open_path(result.report.parent)).pack(side='left')
        ttk.Button(actions, text='打开 CSV 报告', command=lambda: self._open_path(result.report)).pack(side='left', padx=8)
        ttk.Button(actions, text='查看日志', command=self._show_logs).pack(side='left')

    def _open_path(self, path: Path) -> None:
        try:
            if sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif os.name == 'nt':
                os.startfile(str(path))
            else:
                subprocess.Popen(['xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.log.exception('打开路径失败 %s', path)
            self.status_var.set(f'无法打开 {path}：{exc}')

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.exporting and not messagebox.askyesno('结束导出并退出', '退出将取消当前导出，已完成片段会保留。继续退出？', parent=self.root):
            return
        if self._current():
            self._apply(quiet=True)
        self._closing = True
        self._close_started = time.monotonic()
        for after in (self._commit_after, self._save_after, self._media_after):
            if after:
                self.root.after_cancel(after)
        self._commit_after = self._save_after = self._media_after = None
        for lane in ('io', 'media', 'preview', 'tools', 'export'):
            self.workers[lane].cancel()
        self.status_var.set('正在保存工作区并结束后台任务…')
        self._refresh_controls()
        self._finish_close()

    def _finish_close(self) -> None:
        if self._destroyed:
            return
        if not self.recovery_ready:
            self._closing = False
            self.status_var.set('工作区仍在恢复，请稍后关闭。')
            self._refresh_controls()
            return
        if not self.workers['save'].busy and self.persisted_revision != self.revision:
            if self._failed_autosave:
                if not messagebox.askyesno('工作区保存失败', '无法保存最新工作区，错误已写入日志。仍然退出将丢失本次未保存修改。继续？', parent=self.root):
                    self._closing = False
                    self._failed_autosave = False
                    self._refresh_controls()
                    return
                self.persisted_revision = self.revision
            else:
                self._autosave()
        if any(worker.busy for worker in self.workers.values()) or self.exporting or self._insert_pending:
            self.root.after(50, self._finish_close)
            return
        # Process queued completions before deciding whether another save is required.
        if not self.events.empty() or self.persisted_revision != self.revision:
            self.root.after(50, self._finish_close)
            return
        for worker in self.workers.values():
            worker.close()
        self.log.info('正常退出，工作区已保存 revision=%s', self.persisted_revision)
        self._destroyed = True
        for after_id in self.root.tk.call('after', 'info'):
            self.root.after_cancel(after_id)
        self.root.destroy()
