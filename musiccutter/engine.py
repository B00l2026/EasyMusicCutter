"""FFmpeg boundary: bounded workers, cancellation, accurate encoding, safe publish."""
from __future__ import annotations

import csv
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from .models import (Cancelled, Clip, ExportResult, MediaInfo, name_key,
                     output_name, time_label, timestamp, validate_range)

LOG = logging.getLogger('musiccutter')
WAVE_WIDTH, WAVE_HEIGHT = 640, 64
Tools = tuple[str, str, str | None]
Stop = Callable[[], bool]


def proc_flags(low: bool = False) -> int:
    if os.name == 'nt':
        return getattr(subprocess, 'CREATE_NO_WINDOW', 0) | (
            getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0) if low else 0)
    return 0


def get_tools(custom: str = '') -> Tools:
    here = Path(__file__).resolve().parents[1]
    if custom:
        candidates = [Path(custom.strip('"')).expanduser()]
    else:
        candidates = [folder / name for folder in (here / 'bin', here, Path('/opt/homebrew/bin'), Path('/usr/local/bin'))
                      for name in ('ffmpeg', 'ffmpeg.exe')]
        found = shutil.which('ffmpeg')
        if found:
            candidates.insert(2, Path(found))
    for path in candidates:
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        ext = '.exe' if path.suffix.casefold() == '.exe' else ''
        probe, play = path.with_name('ffprobe' + ext), path.with_name('ffplay' + ext)
        probe_str = str(probe) if probe.is_file() and os.access(probe, os.X_OK) else shutil.which('ffprobe')
        play_str = str(play) if play.is_file() and os.access(play, os.X_OK) else shutil.which('ffplay')
        if probe_str:
            return str(path), probe_str, play_str
    raise RuntimeError('未找到可执行的 FFmpeg 和 FFprobe。请放入 bin 文件夹，或在「设置」中指定 FFmpeg。')


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=0.4)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            LOG.exception('无法回收子进程 pid=%s', proc.pid)


def process(args: list[str], *, timeout: float, stop: Stop | None = None,
            low: bool = False, on_progress: Callable[[float], None] | None = None,
            on_started: Callable[[], None] | None = None) -> tuple[int, bytes, bytes]:
    """Call only from workers. Continuously drain pipes; terminate/reap on every exit."""
    if stop and stop():
        raise Cancelled('任务已取消')
    started = time.monotonic()
    reader: threading.Thread | None = None
    output = bytearray()
    # Error output goes to a temporary file, preventing pipe deadlock and unbounded RAM.
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=errors, creationflags=proc_flags(low))
        try:
            if on_started:
                on_started()
            if on_progress:
                def read_progress() -> None:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        if len(output) < 131072:
                            output.extend(line)
                        if line.startswith(b'out_time_us='):
                            try:
                                on_progress(max(0.0, int(line.split(b'=', 1)[1]) / 1e6))
                            except (ValueError, OverflowError):
                                pass
                reader = threading.Thread(target=read_progress, name='mc-progress', daemon=True)
                reader.start()
            while True:
                if stop and stop():
                    raise Cancelled('任务已取消')
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f'操作超过 {timeout:.0f} 秒，已终止音频进程；可重试或查看日志')
                try:
                    if reader:
                        proc.wait(timeout=0.08)
                    else:
                        out, _ = proc.communicate(timeout=0.08)
                        output.extend(out)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if stop and stop():
                raise Cancelled('任务已取消')
        finally:
            _terminate(proc)
            if reader:
                reader.join(timeout=3)
            if proc.stdout:
                proc.stdout.close()
        errors.seek(0, os.SEEK_END)
        errors.seek(max(0, errors.tell() - 16384))
        err = errors.read()
        return proc.returncode or 0, bytes(output), err


def check_tools(tools: Tools, stop: Stop = lambda: False) -> str:
    versions = []
    for executable in tools[:2]:
        rc, out, err = process([executable, '-version'], timeout=6, stop=stop)
        if rc:
            raise RuntimeError(f'无法运行 {executable}：{err.decode("utf-8", "replace")}')
        versions.append(out.decode('utf-8', 'replace').splitlines()[0])
    rc, out, err = process([tools[0], '-hide_banner', '-encoders'], timeout=6, stop=stop)
    if rc or b'libmp3lame' not in out:
        raise RuntimeError('此 FFmpeg 不包含 libmp3lame 编码器，请更换完整版本后重试')
    return '\n'.join(versions)


def probe_audio(ffprobe: str, source: Path, stop: Stop = lambda: False) -> MediaInfo:
    rc, out, err = process([ffprobe, '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=duration,sample_rate,channels,codec_name:format=duration',
        '-of', 'json', str(source)], timeout=15, stop=stop, low=True)
    if rc:
        raise RuntimeError('无法读取音频：' + (err.decode('utf-8', 'replace').strip() or f'退出码 {rc}'))
    try:
        data = json.loads(out)
        stream = data['streams'][0]
        duration = stream.get('duration') or data.get('format', {}).get('duration')
        duration = float(duration)
        rate = int(stream['sample_rate'])
        channels = int(stream['channels'])
        if not math.isfinite(duration) or duration <= 0 or rate <= 0 or channels <= 0:
            raise ValueError()
        return MediaInfo(duration, rate, channels, stream.get('codec_name', '未知'))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError('文件中没有可读取的音频流或有效时长') from exc


def read_duration(ffprobe: str, source: Path, stop: Stop | None = None) -> float:
    return probe_audio(ffprobe, source, stop or (lambda: False)).duration


def decode_wave(ffmpeg: str, source: Path, stop: Stop) -> list[float]:
    filt = (f'[0:a:0]aresample=2000,aformat=channel_layouts=mono,'
            f'showwavespic=s={WAVE_WIDTH}x{WAVE_HEIGHT}:colors=white:filter=peak:draw=full[w]')
    rc, out, err = process([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-threads', '1',
        '-filter_complex_threads', '1', '-i', str(source), '-filter_complex', filt,
        '-map', '[w]', '-frames:v', '1', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'],
        timeout=90, stop=stop, low=True)
    if rc:
        raise RuntimeError('波形生成失败：' + err.decode('utf-8', 'replace').strip())
    if len(out) != WAVE_WIDTH * WAVE_HEIGHT:
        raise RuntimeError('波形数据不完整')
    peaks = [0.0] * WAVE_WIDTH
    half = WAVE_HEIGHT // 2
    for y in range(WAVE_HEIGHT):
        if stop():
            raise Cancelled()
        strength = abs(y - half) / half
        for x in range(WAVE_WIDTH):
            if out[y * WAVE_WIDTH + x] > 75:
                peaks[x] = max(peaks[x], strength)
    top = max(peaks) or 1.0
    return [min(1.0, (value / top) ** 0.7) for value in peaks]


def verify_audio(ffmpeg: str, source: Path, expected: float, stop: Stop,
                 progress: Callable[[float], None]) -> None:
    """Verify decoded duration, not MP3 container duration including encoder padding."""
    decoded = 0.0

    def observed(seconds: float) -> None:
        nonlocal decoded
        decoded = seconds
        progress(seconds)

    rc, _, err = process([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-xerror',
        '-threads', '1', '-i', str(source), '-map', '0:a:0', '-vn', '-progress', 'pipe:1',
        '-nostats', '-f', 'null', '-'], timeout=max(45, expected * 1.5 + 15),
        stop=stop, low=True, on_progress=observed)
    if rc:
        raise RuntimeError('片段解码验证失败：' + err.decode('utf-8', 'replace').strip())
    if decoded <= 0 or abs(decoded - expected) > 0.025:
        raise RuntimeError(f'片段实际时长 {decoded:.3f} 秒，期望 {expected:.1f} 秒；未保存不完整片段')


def export_one(clip: Clip, folder: Path, tools: Tools, overwrite: bool, stop: Stop,
               stage: Callable[[str], None], duration: float | None = None,
               fraction: Callable[[float], None] = lambda p: None,
               protected: set[Path] | None = None) -> str:
    name = output_name(clip.output)
    if not clip.source.is_file():
        raise FileNotFoundError(f'找不到源音频：{clip.source}；请重新定位')
    if duration is None:
        duration = read_duration(tools[1], clip.source, stop)
    start, length = validate_range(clip.start, clip.length, duration)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    resolved = dest.resolve()
    if resolved == clip.source.resolve() or (protected and resolved in protected):
        raise ValueError('输出路径与本批次源音频相同，禁止覆盖源音频')
    if dest.is_symlink():
        raise ValueError('输出目标是符号链接，请更换名称或目录')
    if dest.exists() and not overwrite:
        return '跳过（已存在）'
    suffix = Path(name).suffix.lower()
    handle, temp_name = tempfile.mkstemp(prefix='.musiccutter_', suffix=suffix, dir=folder)
    os.close(handle)
    temp = Path(temp_name)
    try:
        stage('编码')
        codec = {'.mp3': ['-c:a', 'libmp3lame', '-b:a', '192k'],
                 '.flac': ['-c:a', 'flac', '-compression_level', '5'],
                 '.wav': ['-c:a', 'pcm_s16le']}[suffix]
        # Prime MP3 bit reservoirs/AAC decoder state, then trim on the decoded timeline.
        # Direct input-only seeking can change the first frames even with accurate_seek.
        seek = max(0.0, start - 2.0)
        input_args = (['-ss', f'{seek:.1f}'] if seek > 0 else []) + ['-i', str(clip.source)]
        rc, _, err = process([tools[0], '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-y',
            '-threads', '1', *input_args, '-ss', f'{start - seek:.1f}', '-t', f'{length:.1f}',
            '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1', *codec,
            '-threads', '1', '-progress', 'pipe:1', '-nostats', str(temp)], timeout=max(75, length * 1.5 + 30),
            stop=stop, low=True, on_progress=lambda seconds: fraction(min(0.86, seconds / length * 0.86)))
        if rc:
            raise RuntimeError('音频编码失败：' + (err.decode('utf-8', 'replace').strip() or f'退出码 {rc}'))
        if temp.stat().st_size < 32:
            raise RuntimeError('生成的片段为空')
        stage('验证')
        verify_audio(tools[0], temp, length, stop,
                     lambda seconds: fraction(0.86 + min(1.0, seconds / length) * 0.12))
        if stop():
            raise Cancelled()
        stage('写入')
        if overwrite:
            # Recheck symlinks and protected paths immediately before publishing.
            if dest.is_symlink() or dest.resolve() == clip.source.resolve() or (protected and dest.resolve() in protected):
                raise ValueError('输出路径发生变化，已停止写入以保护源音频')
            os.replace(temp, dest)
        else:
            # link is an atomic no-clobber publish; an intervening writer is never overwritten.
            try:
                os.link(temp, dest)
            except FileExistsError:
                return '跳过（已存在）'
        fraction(1.0)
        return '成功'
    finally:
        temp.unlink(missing_ok=True)


def _csv_text(value: Any) -> str:
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def _report_time(value: Any) -> str:
    try:
        return time_label(timestamp(value))
    except (ValueError, TypeError, OverflowError):
        return str(value)


def run_batch(clips: list[Clip], folder: Path, tools: Tools, overwrite: bool,
              cancel: threading.Event, progress: Callable[[str, Any], None], *,
              protected_sources: set[Path] | None = None) -> ExportResult:
    """Every submitted task appears in the report, including cancelled/unstarted ones."""
    folder.mkdir(parents=True, exist_ok=True)
    # Reserve the report before exporting, so an unwritable directory fails before work.
    fd, report_temp = tempfile.mkstemp(prefix='.musiccutter_report_', suffix='.csv', dir=folder)
    report = folder / ('处理报告_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.csv')
    counts = {'成功': 0, '跳过（已存在）': 0, '失败': 0, '已取消': 0, '未开始': 0}
    protected: set[Path] = set()
    for source in {c.source for c in clips} | (protected_sources or set()):
        try:
            protected.add(source.resolve())
        except (OSError, RuntimeError):
            protected.add(source.absolute())
    keys: dict[str, int] = {}
    for c in clips:
        key = name_key(c.output)
        keys[key] = keys.get(key, 0) + 1
    cache: dict[tuple[Path, int, int], float] = {}
    cancelled = False
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8-sig') as file:
            writer = csv.writer(file)
            writer.writerow(['源音频', '开始时间', '片段秒数', '输出文件', '状态', '详情'])
            for index, clip in enumerate(clips, 1):
                status, detail = '未开始', '取消后未开始'
                if not cancel.is_set():
                    def stage(text: str) -> None:
                        progress('export_stage', (index, len(clips), clip.uid, text))
                    try:
                        stage('检查')
                        if keys[name_key(clip.output)] > 1:
                            raise ValueError('输出文件名重复；本批次同名任务均未导出，请修改名称')
                        stat = clip.source.stat()
                        key = (clip.source.resolve(), stat.st_mtime_ns, stat.st_size)
                        dur = cache.get(key)
                        if dur is None:
                            dur = read_duration(tools[1], clip.source, cancel.is_set)
                            cache[key] = dur
                        if clip.auto_length:
                            clip.length = min(clip.length, math.floor((dur + 1e-6) * 10) / 10)
                            clip.auto_length = False
                            progress('export_range', (clip.uid, clip.start, clip.length))
                        status = export_one(clip, folder, tools, overwrite, cancel.is_set, stage, dur,
                            lambda fraction: progress('export_progress', (index, len(clips), clip.uid, fraction)), protected)
                        detail = str(folder / clip.output) if status == '成功' else '已有同名文件，未覆盖'
                    except Cancelled:
                        cancelled, status, detail = True, '已取消', '已清理当前片段临时文件'
                    except Exception as exc:
                        status, detail = '失败', str(exc)
                        LOG.exception('导出失败 uid=%s source=%s output=%s', clip.uid, clip.source, clip.output)
                else:
                    cancelled = True
                counts[status] += 1
                writer.writerow([_csv_text(clip.source), _report_time(clip.start), _csv_text(clip.length),
                                 _csv_text(clip.output), status, _csv_text(detail)])
                file.flush()
                progress('export_row', (index, len(clips), clip.uid, status, detail))
            os.fsync(file.fileno())
        os.replace(report_temp, report)
    finally:
        Path(report_temp).unlink(missing_ok=True)
    return ExportResult(counts['成功'], counts['跳过（已存在）'], counts['失败'], counts['已取消'],
                        counts['未开始'], report, cancelled)


def batch_export(clips: list[Clip], folder: Path, tools: Tools, overwrite: bool,
                 cancel: threading.Event, progress: Callable[[str, Any], None]) -> tuple[int, int, int, Path, bool]:
    """Compatibility API for v3 command line clients."""
    result = run_batch(clips, folder, tools, overwrite, cancel, progress)
    return result.success, result.skipped, result.failed, result.report, result.was_cancelled
