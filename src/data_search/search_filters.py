"""Validated, parameterized filters shared by lexical and semantic retrieval."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .extractors import TEXT_EXTENSIONS

CATEGORIES = {
    'document': {'.pdf', '.docx', '.pptx', '.txt', '.md', '.markdown', '.rst', '.tex'},
    'spreadsheet': {'.csv', '.tsv', '.xlsx'},
    'code': TEXT_EXTENSIONS - {'.txt', '.md', '.markdown', '.log', '.rst', '.tex', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.ini', '.xml', '.conf', '.config', '.env'},
    'data': {'.json', '.jsonl', '.xml', '.csv', '.tsv', '.yaml', '.yml', '.toml', '.ini', '.conf', '.config', '.env'},
    'image': {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.heic'},
    'audio': {'.mp3', '.wav', '.flac', '.m4a', '.ogg'},
    'video': {'.mp4', '.mkv', '.mov', '.avi', '.webm'},
    'archive': {'.zip', '.7z', '.rar', '.tar', '.gz'},
}


def path_predicate(directory, alias='d'):
    path = str(Path(directory).expanduser().resolve())
    prefix = path.rstrip('\\/') + os.sep
    if os.name!='nt':
        # LIKE folds ASCII case even on case-sensitive Linux filesystems.
        return f" AND {alias}.source_id='files' AND ({alias}.path=? OR ({alias}.path>=? COLLATE BINARY AND {alias}.path<? COLLATE BINARY))", [path,prefix,prefix[:-1]+chr(ord(prefix[-1])+1)]
    literal = prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f" AND {alias}.source_id='files' AND ({alias}.path=? OR {alias}.path LIKE ? ESCAPE '\\')", [path, literal+'%']


def _date(value):
    if not isinstance(value, str):
        raise ValueError('date must be ISO 8601 or YYYY-MM-DD')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp()*1_000_000_000)


def build_filters(*, source_id=None, extension=None, extensions=None, directory=None,
                  modified_after=None, modified_before=None, min_size=None, max_size=None,
                  category=None, sort='relevance'):
    if sort not in {'relevance', 'modified_desc', 'modified_asc', 'name'}:
        raise ValueError('invalid sort')
    sql, args, applied = '', [], {'sort': sort}
    if source_id:
        sql += ' AND d.source_id=?'
        args.append(source_id)
        applied['source_id'] = source_id
    for label, values in [('extension', [extension] if extension else []), ('extensions', extensions or [])]:
        if not isinstance(values, (list, tuple)) or len(values) > 64:
            raise ValueError('extensions must be a list of at most 64 extensions')
        if values:
            if any(not isinstance(v, str) or not v.startswith('.') or len(v)>24 or any(c in v for c in '/\\%*') for v in values):
                raise ValueError('extensions must start with a dot and contain no wildcards or paths')
            normalized = sorted(set(v.lower() for v in values))
            sql += ' AND d.extension IN ('+','.join('?' for _ in normalized)+')'
            args.extend(normalized)
            applied[label] = normalized[0] if label=='extension' else normalized
    if category:
        if category not in CATEGORIES:
            raise ValueError('unknown file category')
        values = sorted(CATEGORIES[category])
        sql += " AND d.source_id='files' AND d.extension IN ("+','.join('?' for _ in values)+')'
        args.extend(values)
        applied['category'] = category
    if directory:
        clause, values = path_predicate(directory)
        sql += clause
        args.extend(values)
        applied['directory'] = values[0]
    dates = {}
    for key, value, op in [('modified_after', modified_after, '>='), ('modified_before', modified_before, '<')]:
        if value is not None:
            dates[key] = _date(value)
            sql += f" AND d.source_id='files' AND d.mtime_ns{op}?"
            args.append(dates[key])
            applied[key] = value
    if len(dates)==2 and dates['modified_after']>=dates['modified_before']:
        raise ValueError('modified_after must precede modified_before (exclusive upper bound)')
    for key, value, op in [('min_size', min_size, '>='), ('max_size', max_size, '<=')]:
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value<0:
                raise ValueError('size filters must be nonnegative integer bytes')
            sql += f" AND d.source_id='files' AND d.size{op}?"
            args.append(value)
            applied[key] = value
    if min_size is not None and max_size is not None and min_size>max_size:
        raise ValueError('min_size exceeds max_size')
    return sql, args, applied
