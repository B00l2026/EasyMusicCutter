"""Versioned project files with atomic writes and recoverable editor drafts."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import Clip, output_name, validate_range

SCHEMA = 1
MAX_PROJECT_BYTES = 32 * 1024 * 1024


@dataclass
class Project:
    clips: list[Clip] = field(default_factory=list)
    output_dir: Path | None = None
    drafts: dict[int, dict[str, str]] = field(default_factory=dict)
    ffmpeg: str = ''
    recursive: bool = True


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.musiccutter_', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _relative(path: Path, base: Path) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:  # different drive on Windows
        return str(path)


def save_project(path: Path, project: Project) -> None:
    rows = []
    for clip in project.clips:
        start, length = validate_range(clip.start, clip.length)
        rows.append({'source': _relative(clip.source, path.parent), 'start': start,
                     'length': length, 'output': output_name(clip.output),
                     'draft': project.drafts.get(clip.uid), 'auto_length': clip.auto_length})
    atomic_json(path, {'schema': SCHEMA, 'application': 'MusicCutter', 'clips': rows,
                      'output_dir': _relative(project.output_dir, path.parent) if project.output_dir else None,
                      'ffmpeg': project.ffmpeg, 'recursive': project.recursive})


def load_project(path: Path) -> Project:
    if path.stat().st_size > MAX_PROJECT_BYTES:
        raise ValueError('项目文件超过 32 MB，无法安全打开')
    with path.open(encoding='utf-8') as file:
        data = json.load(file)
    if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != SCHEMA:
        raise ValueError('项目版本不兼容或文件格式不正确')
    if not isinstance(data.get('clips'), list):
        raise ValueError('项目缺少片段列表')
    project = Project()
    for index, row in enumerate(data['clips'], 1):
        try:
            if not isinstance(row, dict) or not isinstance(row.get('source'), str) or not row['source']:
                raise ValueError('源路径无效')
            start, length = validate_range(row['start'], row['length'])
            name = output_name(row['output'])
            source = (path.parent / Path(row['source']).expanduser()).resolve()
            clip = Clip(source.name, source, start, length, name)
            clip.auto_length = row.get('auto_length', False)
            if not isinstance(clip.auto_length, bool):
                raise ValueError('默认时长标记无效')
            project.clips.append(clip)
            draft = row.get('draft')
            if draft is not None:
                if not isinstance(draft, dict) or any(not isinstance(draft.get(k), str) for k in ('start', 'length', 'output')):
                    raise ValueError('编辑草稿格式不正确')
                project.drafts[clip.uid] = {k: draft[k] for k in ('start', 'length', 'output')}
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ValueError(f'项目第 {index} 个片段：{exc}') from exc
    output = data.get('output_dir')
    if output is not None and not isinstance(output, str):
        raise ValueError('输出目录格式不正确')
    project.output_dir = (path.parent / output).resolve() if output else None
    project.ffmpeg = data.get('ffmpeg', '')
    project.recursive = data.get('recursive', True)
    if not isinstance(project.ffmpeg, str) or not isinstance(project.recursive, bool):
        raise ValueError('项目设置格式不正确')
    return project
