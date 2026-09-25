"""Read existing NTFS change journals without changing privileges or disk state.

The caller must atomically persist ``next_cursor`` with the returned events AND
the reconciliation flag. A new/reset cursor anchors *before* a baseline scan;
subsequent polls replay changes made while that scan was running. This module
never creates journals, enumerates the MFT, or assumes a journal replaces a
periodic reconciliation. Paths are resolved using the current account token.

Win32 layouts/API contracts:
https://learn.microsoft.com/windows/win32/api/winioctl/ns-winioctl-usn_record_v2
https://learn.microsoft.com/windows/win32/api/winioctl/ns-winioctl-usn_record_v3
https://learn.microsoft.com/windows/win32/api/winioctl/ns-winioctl-read_usn_journal_data_v1
https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-openfilebyid
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import struct


FILE_CREATE = 0x00000100
FILE_DELETE = 0x00000200
SECURITY_CHANGE = 0x00000800
RENAME_OLD_NAME = 0x00001000
RENAME_NEW_NAME = 0x00002000
HARD_LINK_CHANGE = 0x00010000
REPARSE_POINT_CHANGE = 0x00100000
DIRECTORY = 0x10
REPARSE_POINT = 0x400
# All documented changes except close-only notifications. Reading without
# ReturnOnlyOnClose also observes changes to files kept open by an application.
REASON_MASK = 0x00FFFF77
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
_V2 = struct.Struct('<IHHQQqqIIIIHH')
_V3 = struct.Struct('<IHH16s16sqqIIIIHH')


class JournalError(OSError):
    """A bounded diagnostic; do not expose native error strings or paths."""

    def __init__(self, reason: str, code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class JournalInfo:
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int = 0

    def cursor(self) -> dict:
        return {'journal_id': str(self.journal_id), 'next_usn': self.next_usn}


@dataclass(frozen=True)
class UsnRecord:
    file_id: str
    parent_id: str
    usn: int
    reason: int
    attributes: int
    name: str


@dataclass(frozen=True)
class JournalBatch:
    next_usn: int
    records: list[UsnRecord]
    has_more: bool


def _record_at(data: bytes, offset: int) -> tuple[UsnRecord, int]:
    if len(data) - offset < 8:
        raise JournalError('truncated_record')
    length, major, _minor = struct.unpack_from('<IHH', data, offset)
    if major not in (2, 3):
        raise JournalError('unsupported_record_version')
    layout = _V2 if major == 2 else _V3
    if length < layout.size or length % 8 or offset + length > len(data):
        raise JournalError('invalid_record_length')
    values = layout.unpack_from(data, offset)
    file_id, parent_id, usn = values[3:6]
    name_length, name_offset = values[-2:]
    if (usn < 0 or name_offset < layout.size or name_offset % 2 or
            not name_length or name_length % 2 or name_offset + name_length > length):
        raise JournalError('invalid_record_fields')
    try:
        name = data[offset + name_offset:offset + name_offset + name_length].decode('utf-16-le')
    except UnicodeDecodeError:
        raise JournalError('invalid_filename_encoding') from None
    # IDs are opaque, fixed-width hex; V3 retains its 16-byte native ordering.
    if major == 2:
        file_id, parent_id = f'{file_id:016x}', f'{parent_id:016x}'
    else:
        file_id, parent_id = file_id.hex(), parent_id.hex()
    return UsnRecord(file_id, parent_id, usn, values[7], values[10], name), offset + length


def parse_usn_buffer(data: bytes, max_records: int = 256) -> JournalBatch:
    """Decode a bounded read; leave the cursor at the first unconsumed record.

The first eight bytes are the kernel's next USN, not a record. Record names use
their runtime offsets, including when a future minor version adds header fields.
"""
    if not 1 <= max_records <= 4096:
        raise ValueError('max_records must be between 1 and 4096')
    if len(data) < 8:
        raise JournalError('truncated_buffer')
    kernel_next, = struct.unpack_from('<q', data)
    if kernel_next < 0:
        raise JournalError('invalid_next_usn')
    offset, previous, records = 8, -1, []
    while offset < len(data):
        record, following = _record_at(data, offset)
        if record.usn <= previous or record.usn >= kernel_next:
            raise JournalError('invalid_usn_order')
        if len(records) == max_records:
            return JournalBatch(record.usn, records, True)
        records.append(record)
        offset, previous = following, record.usn
    return JournalBatch(kernel_next, records, False)


def _volume_root(value: str) -> str:
    value = str(value).replace('/', '\\')
    if len(value) != 3 or not value[0].isascii() or not value[0].isalpha() or value[1:] != ':\\':
        raise JournalError('drive_root_required')
    return value[0].upper() + ':\\'


def _record_path(volume: str, parent: str, name: str) -> str:
    # Do not accept a journal name as an arbitrary path/ADS/device namespace.
    reserved = name.split('.', 1)[0].upper()
    if (not name or name in ('.', '..') or any(c in name for c in '\\/:\0?*"<>|') or
            any(ord(c) < 32 for c in name) or name.endswith((' ', '.')) or
            reserved in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} or
            (len(reserved) == 4 and reserved[:3] in ('COM', 'LPT') and reserved[3] in '123456789¹²³')):
        raise JournalError('unsafe_record_name')
    if parent.startswith('\\\\?\\'):
        parent = parent[4:]
    if not ntpath.isabs(parent) or ntpath.splitdrive(parent)[0].casefold() != volume[:2].casefold():
        raise JournalError('parent_outside_volume')
    parent = ntpath.normpath(parent)
    path = ntpath.join(parent, name)
    if ntpath.commonpath((volume, path)).casefold() != volume.casefold():
        raise JournalError('parent_outside_volume')
    return path


def _error_reason(error: OSError) -> str:
    if isinstance(error, JournalError):
        return error.reason
    code = getattr(error, 'winerror', None) or getattr(error, 'errno', None)
    return {
        1: 'journal_unsupported', 5: 'access_denied', 32: 'volume_busy',
        87: 'journal_unsupported', 1178: 'journal_deleting',
        1179: 'journal_not_active', 1181: 'journal_entries_lost',
    }.get(code, 'journal_io_error')


class WindowsJournalBackend:
    """Read-only Win32 transport, opened lazily so fallback is observable."""

    def __init__(self, volume_root: str):
        self.volume = _volume_root(volume_root)
        self.handle = None
        if os.name != 'nt':
            raise JournalError('platform_unsupported')
        from ctypes import wintypes as wt
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        declarations = {
            'CreateFileW': ([wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE], wt.HANDLE),
            'CloseHandle': ([wt.HANDLE], wt.BOOL),
            'DeviceIoControl': ([wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p], wt.BOOL),
            'OpenFileById': ([wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD], wt.HANDLE),
            'GetFinalPathNameByHandleW': ([wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD], wt.DWORD),
            'GetVolumeInformationW': ([wt.LPCWSTR, wt.LPWSTR, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                       ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), wt.LPWSTR, wt.DWORD], wt.BOOL),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result

    def _open(self):
        if self.handle is not None:
            return
        filesystem = ctypes.create_unicode_buffer(32)
        if not self.kernel.GetVolumeInformationW(self.volume, None, 0, None, None, None, filesystem, len(filesystem)):
            raise ctypes.WinError(ctypes.get_last_error())
        if filesystem.value.upper() != 'NTFS':
            raise JournalError('filesystem_not_ntfs')
        # No write access, no token changes, and no journal-control mutation API.
        handle = self.kernel.CreateFileW('\\\\.\\' + self.volume[:2], 0x80000000, 7, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle

    def _ioctl(self, code: int, request: bytes | None, output_size: int) -> bytes:
        from ctypes import wintypes as wt
        self._open()
        output = ctypes.create_string_buffer(output_size)
        source = ctypes.create_string_buffer(request) if request is not None else None
        returned = wt.DWORD()
        success = self.kernel.DeviceIoControl(self.handle, code, source, len(request) if request else 0,
                                             output, output_size, ctypes.byref(returned), None)
        if not success:
            error = ctypes.get_last_error()
            # A complete partial batch is safe; the decoder rejects truncation.
            if error != 234 or not returned.value:
                self.close()  # Retry discovery if a volume is later reattached.
                raise ctypes.WinError(error)
        if returned.value > output_size:
            raise JournalError('invalid_output_length')
        return output.raw[:returned.value]

    def query(self) -> JournalInfo:
        data = self._ioctl(FSCTL_QUERY_USN_JOURNAL, None, 128)
        if len(data) < 56:
            raise JournalError('invalid_journal_info')
        journal_id, first, next_usn, lowest, _maximum, _size, _delta = struct.unpack_from('<QqqqqQQ', data)
        if min(first, next_usn, lowest) < 0 or first > next_usn or lowest > next_usn:
            raise JournalError('invalid_journal_info')
        return JournalInfo(journal_id, first, next_usn, lowest)

    def read(self, start_usn: int, journal_id: int, max_bytes: int) -> bytes:
        # READ_USN_JOURNAL_DATA_V1 has four trailing alignment bytes on Win32/64.
        # BytesToWaitFor=0 makes reads return at EOF instead of waiting for writes.
        request = struct.pack('<qIIQQQHH4x', start_usn, REASON_MASK, 0, 0, 0, journal_id, 2, 3)
        return self._ioctl(FSCTL_READ_USN_JOURNAL, request, max_bytes)

    def resolve(self, file_id: str) -> str:
        self._open()
        # FILE_ID_DESCRIPTOR: DWORD size, enum type, 16-byte aligned union.
        if len(file_id) == 16:
            descriptor = struct.pack('<IIQ8x', 24, 0, int(file_id, 16))
        elif len(file_id) == 32:
            descriptor = struct.pack('<II16s', 24, 2, bytes.fromhex(file_id))
        else:
            raise JournalError('invalid_file_id')
        source = ctypes.create_string_buffer(descriptor)
        handle = self.kernel.OpenFileById(self.handle, source, 0x80, 7, None, 0x02000000 | 0x00200000)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = 512
            for _ in range(2):
                buffer = ctypes.create_unicode_buffer(size)
                length = self.kernel.GetFinalPathNameByHandleW(handle, buffer, size, 0)
                if not length:
                    raise ctypes.WinError(ctypes.get_last_error())
                if length < size:
                    return buffer.value
                if length > 32768:
                    raise JournalError('resolved_path_too_long')
                size = length + 1
            raise JournalError('resolved_path_changed')
        finally:
            self.kernel.CloseHandle(handle)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class VolumeJournal:
    """Stateless cursor controller; injected backends keep tests off real disks.

``max_events`` bounds examined records (even excluded ones); ``max_bytes``
bounds the single native read. No cache of parent paths survives a poll, because
directory renames would invalidate it. ``allowed`` receives a pathlib.Path.
"""

    def __init__(self, volume_root: str, backend=None):
        self.volume = str(volume_root)
        self.backend = backend
        self.error = None
        try:
            self.volume = _volume_root(self.volume)
            if self.backend is None:
                self.backend = WindowsJournalBackend(self.volume)
        except OSError as error:
            self.error = _error_reason(error)

    def close(self):
        if self.backend is not None:
            self.backend.close()

    def poll(self, cursor=None, *, max_events=256, max_bytes=262144, allowed=None) -> dict:
        if not isinstance(max_events, int) or isinstance(max_events, bool) or not 1 <= max_events <= 4096:
            raise ValueError('max_events must be between 1 and 4096')
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 4096 <= max_bytes <= 1048576:
            raise ValueError('max_bytes must be between 4096 and 1048576')
        result = {
            'available': False, 'backend': 'ntfs_usn', 'volume': self.volume,
            'events': [], 'next_cursor': cursor, 'reconciliation_required': True,
            'reason': self.error, 'has_more': False, 'diagnostics': [], 'records_examined': 0,
        }
        if self.error:
            return result
        try:
            info = self.backend.query()
        except OSError as error:
            result['reason'] = _error_reason(error)
            return result
        result.update(available=True, next_cursor=info.cursor(), reason='initial_baseline_required')
        if cursor is None:
            return result
        if (not isinstance(cursor, dict) or not isinstance(cursor.get('journal_id'), str) or
                not isinstance(cursor.get('next_usn'), int) or isinstance(cursor.get('next_usn'), bool)):
            result['reason'] = 'invalid_cursor'
            return result
        if cursor['journal_id'] != str(info.journal_id):
            result['reason'] = 'journal_reset'
            return result
        start = cursor['next_usn']
        if start < max(info.first_usn, info.lowest_valid_usn):
            result['reason'] = 'journal_entries_lost'
            return result
        if start > info.next_usn:
            result['reason'] = 'cursor_ahead_of_journal'
            return result
        try:
            raw = self.backend.read(start, info.journal_id, max_bytes)
            if len(raw) > max_bytes:
                raise JournalError('read_budget_exceeded')
            batch = parse_usn_buffer(raw, max_events)
            if batch.next_usn < start or any(record.usn < start for record in batch.records):
                raise JournalError('cursor_went_backwards')
        except OSError as error:
            result.update(available=False, next_cursor=cursor, reason=_error_reason(error))
            # A reset can race query/read. Re-anchor before the recovery scan.
            if not isinstance(error, JournalError) or result['reason'] == 'journal_entries_lost':
                try:
                    current = self.backend.query()
                    if current.journal_id != info.journal_id or start < max(current.first_usn, current.lowest_valid_usn):
                        result.update(available=True, next_cursor=current.cursor(), reason='journal_reset_during_read')
                except OSError:
                    pass
            return result
        result.update(next_cursor={'journal_id': str(info.journal_id), 'next_usn': batch.next_usn},
                      reconciliation_required=False, reason=None,
                      has_more=batch.has_more or batch.next_usn < info.next_usn,
                      records_examined=len(batch.records))
        # Resolving a parent once per batch avoids repeated handles without an
        # unbounded cross-poll cache or stale directory paths after a rename.
        parents = {}
        for record in batch.records:
            try:
                if record.parent_id not in parents:
                    try:
                        parents[record.parent_id] = self.backend.resolve(record.parent_id)
                    except OSError:
                        parents[record.parent_id] = None
                parent = parents[record.parent_id]
                if parent is None:
                    raise JournalError('parent_unresolved')
                path = _record_path(self.volume, parent, record.name)
                if allowed is not None and not allowed(Path(path)):
                    continue
            except (OSError, ValueError) as error:
                result['reconciliation_required'] = True
                result['reason'] = 'unresolved_changes'
                if len(result['diagnostics']) < 8:
                    result['diagnostics'].append({'reason': _error_reason(error), 'usn': record.usn})
                continue
            directory = bool(record.attributes & DIRECTORY)
            ambiguous = bool(record.reason & (HARD_LINK_CHANGE | REPARSE_POINT_CHANGE) or
                             record.attributes & REPARSE_POINT or
                             record.reason & RENAME_OLD_NAME and record.reason & RENAME_NEW_NAME)
            if directory or ambiguous:
                action = 'reconcile'
                result.update(reconciliation_required=True, reason='structural_changes')
            elif record.reason & (FILE_DELETE | RENAME_OLD_NAME):
                action = 'delete'
            else:
                action = 'upsert'
            result['events'].append({'action': action, 'path': path, 'is_directory': directory,
                                     'file_id': record.file_id, 'parent_id': record.parent_id, 'usn': record.usn})
        return result
