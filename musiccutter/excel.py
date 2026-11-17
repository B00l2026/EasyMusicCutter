"""Optional v3 Excel interoperability; the normal editing flow needs no Excel."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from .models import Clip, output_name, time_label, validate_range

HEADERS = ['原始文件名', '开始时间', '截取秒数', '输出文件名']
ALIASES = [
    {'原始文件名', '音频文件名', '文件名', 'filename', 'source', 'file'},
    {'开始时间', '截取开始时间', '开始位置', 'start'},
    {'截取秒数', '截取时长', '剪辑时长', '时长', 'duration', 'length'},
    {'输出文件名', '片段文件名', '保存文件名', 'output'},
]


def _openpyxl():
    try:
        import openpyxl
        return openpyxl
    except ImportError as exc:
        raise RuntimeError('Excel 兼容功能需要 openpyxl。请运行 python3 -m pip install -r requirements.txt') from exc


def source_from_excel(name: str, audio_dir: Path | None, excel: Path) -> Path:
    path = Path(name.strip()).expanduser()
    basename = re.split(r'[/\\]', name)[-1]
    paths = ([audio_dir / basename] if audio_dir else []) + [excel.parent / path, excel.parent / 'input' / basename]
    for candidate in paths:
        if candidate.is_file():
            return candidate.resolve()
    return paths[0].resolve()


def load_excel(path: Path, audio_dir: Path | None = None) -> list[Clip]:
    book = _openpyxl().load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book['裁剪任务'] if '裁剪任务' in book.sheetnames else book.active
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            raise ValueError('Excel 表格为空')
        normalize = lambda s: str(s or '').casefold().replace(' ', '').replace('_', '')
        normalized = [normalize(v) for v in header]
        indices = []
        for aliases in ALIASES:
            hits = [i for i, value in enumerate(normalized) if value in {normalize(a) for a in aliases}]
            if len(hits) != 1:
                raise ValueError('Excel 首行需要：' + '、'.join(HEADERS))
            indices.append(hits[0])
        clips = []
        for index, row in enumerate(rows, 2):
            values = [row[i] if i < len(row) else None for i in indices]
            if all(value is None or str(value).strip() == '' for value in values):
                continue
            try:
                filename = str(values[0] or '').strip()
                if not filename:
                    raise ValueError('原始文件名为空')
                start, length = validate_range(values[1], values[2])
                source = source_from_excel(filename, audio_dir, path)
                clips.append(Clip(source.name, source, start, length, output_name(values[3])))
            except (ValueError, TypeError) as exc:
                raise ValueError(f'Excel 第 {index} 行：{exc}') from exc
        return clips
    finally:
        book.close()


def save_excel(path: Path, clips: list[Clip], audio_root: Path | None = None) -> None:
    book = _openpyxl().Workbook()
    sheet = book.active
    sheet.title = '裁剪任务'
    sheet.append(HEADERS)
    sheet.freeze_panes = 'A2'
    for clip in clips:
        try:
            source = os.path.relpath(clip.source, path.parent)
        except ValueError:
            source = str(clip.source)
        sheet.append([source, time_label(clip.start), clip.length, clip.output])
        # Do not turn filenames beginning with '=' into spreadsheet formulas.
        for column in (1, 2, 4):
            sheet.cell(sheet.max_row, column).data_type = 's'
    for column, width in zip('ABCD', (55, 18, 16, 45)):
        sheet.column_dimensions[column].width = width
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.musiccutter_', suffix='.xlsx', dir=path.parent)
    os.close(fd)
    try:
        book.save(tmp)
        os.replace(tmp, path)
    finally:
        book.close()
        Path(tmp).unlink(missing_ok=True)
