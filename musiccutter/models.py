"""Pure validation and models. Times are quantized through integer tenths."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import itertools
import math
from pathlib import Path
import re
import unicodedata
from typing import Any

SUPPORTED = frozenset({'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.aiff', '.aif', '.wma'})
OUTPUT_FORMATS = ('mp3', 'flac', 'wav')
MIN_LENGTH, DEFAULT_LENGTH = 0.1, 15.0
SERIAL = itertools.count(1)


class Cancelled(Exception):
    """A cooperative cancellation, never a processing failure."""


def norm_tenth(value: Any) -> float:
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError('时间必须为有限数字')
        return float((number * 10).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / 10)
    except (InvalidOperation, TypeError, OverflowError) as exc:
        raise ValueError('时间必须是有效数字') from exc


def timestamp(value: Any) -> float:
    """Seconds, MM:SS.s, HH:MM:SS.s, or Excel time. Reject malformed fields."""
    if value is None or isinstance(value, bool):
        raise ValueError('时间不能为空')
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    elif isinstance(value, (time, datetime)):
        t = value.time() if isinstance(value, datetime) else value
        seconds = t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
    elif isinstance(value, (int, float, Decimal)):
        seconds = value
    else:
        text = str(value).strip().replace('：', ':')
        fields = text.split(':')
        if len(fields) > 3 or not text:
            raise ValueError('请输入秒数、分:秒或时:分:秒，例如 01:20.5')
        try:
            if len(fields) == 1:
                seconds = Decimal(fields[0])
            else:
                if any(not re.fullmatch(r'\d+', p) for p in fields[:-1]):
                    raise ValueError()
                if not re.fullmatch(r'\d{1,2}(?:\.\d+)?', fields[-1]):
                    raise ValueError()
                last = Decimal(fields[-1])
                if last >= 60 or (len(fields) == 3 and int(fields[-2]) >= 60):
                    raise ValueError()
                seconds = Decimal(0)
                for part in fields:
                    seconds = seconds * 60 + Decimal(part)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError('时间格式不正确；冒号后的分、秒必须小于 60') from exc
    try:
        if not math.isfinite(float(seconds)) or seconds < 0:
            raise ValueError('时间必须是大于或等于 0 的有限数字')
    except (TypeError, OverflowError) as exc:
        raise ValueError('时间必须是有效数字') from exc
    return norm_tenth(seconds)


def time_label(seconds: float) -> str:
    tenths = max(0, int(round(seconds * 10)))
    minutes, rest = divmod(tenths, 600)
    secs, tenth = divmod(rest, 10)
    return f'{minutes:02d}:{secs:02d}.{tenth}'


def name_key(value: str) -> str:
    # HFS/APFS and Windows can treat different spelling as the same filename.
    return unicodedata.normalize('NFC', value).casefold()


def output_name(value: Any) -> str:
    name = str(value or '').strip()
    if not name or name in {'.', '..'} or name.endswith('.'):
        raise ValueError('输出文件名不能为空，且不能以句点结尾')
    if len(name.encode('utf-8')) > 240:
        raise ValueError('输出文件名过长，请缩短到 240 字节以内')
    if any(c in '<>:"/\\|?*' or ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('输出文件名不能包含路径、控制字符或 <>:"/\\|?*')
    if Path(name).suffix.lower().lstrip('.') not in OUTPUT_FORMATS:
        raise ValueError('输出后缀应为 .mp3、.flac 或 .wav')
    if name.split('.')[0].casefold() in {'con', 'prn', 'aux', 'nul'} | {
        f'{prefix}{i}' for prefix in ('com', 'lpt') for i in range(1, 10)
    }:
        raise ValueError('这是系统保留文件名，请换一个名称')
    return name


def suggested_name(source: Path, fmt: str = 'mp3') -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', source.stem).strip('. ') or '音频'
    while len(stem.encode('utf-8')) > 180:
        stem = stem[:-1]
    return f'{stem}_片段.{fmt}'


def unique_name(proposed: str, used: set[str]) -> str:
    candidate, n = proposed, 2
    while name_key(candidate) in used:
        candidate = f'{Path(proposed).stem}_{n}{Path(proposed).suffix}'
        n += 1
    used.add(name_key(candidate))
    return candidate


def validate_range(start: Any, length: Any, duration: float | None = None) -> tuple[float, float]:
    start = timestamp(start)
    length = timestamp(length)
    if length < MIN_LENGTH:
        raise ValueError('片段时长至少为 0.1 秒')
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError('音频时长无效')
    if duration is not None and start + length > duration + 0.001:
        raise ValueError(f'片段结束 {time_label(start + length)} 超出音频时长 {time_label(duration)}')
    return start, length


@dataclass
class Clip:
    filename: str
    source: Path
    start: float = 0.0
    length: float = DEFAULT_LENGTH
    output: str = ''
    state: str = '待检查'
    note: str = ''
    uid: int = field(default_factory=lambda: next(SERIAL))
    auto_length: bool = False

    @property
    def end(self) -> float:
        return norm_tenth(self.start + self.length)

    def snapshot(self) -> Clip:
        return replace(self)


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    sample_rate: int
    channels: int
    codec: str


@dataclass
class ExportResult:
    success: int
    skipped: int
    failed: int
    cancelled: int
    pending: int
    report: Path
    was_cancelled: bool
