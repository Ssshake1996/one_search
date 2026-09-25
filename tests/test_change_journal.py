"""All replay tests use a synthetic journal; none scan or replay a real disk."""
import os
from pathlib import Path
import struct

import pytest

from data_search.change_journal import (
    DIRECTORY, FILE_CREATE, FILE_DELETE, HARD_LINK_CHANGE, JournalError,
    JournalInfo, RENAME_NEW_NAME, RENAME_OLD_NAME, REPARSE_POINT_CHANGE,
    VolumeJournal, WindowsJournalBackend, _record_path, parse_usn_buffer,
)


def record(name='hello.txt', *, usn=100, reason=FILE_CREATE, file_id=11,
           parent_id=7, attributes=0, version=2, extra_header=b''):
    encoded = name.encode('utf-16-le')
    layout = '<IHHQQqqIIIIHH' if version == 2 else '<IHH16s16sqqIIIIHH'
    offset = struct.calcsize(layout) + len(extra_header)
    length = (offset + len(encoded) + 7) // 8 * 8
    if version == 3:
        file_id = file_id.to_bytes(16, 'little')
        parent_id = parent_id.to_bytes(16, 'little')
    header = struct.pack(layout, length, version, 0, file_id, parent_id, usn, 0,
                         reason, 0, 0, attributes, len(encoded), offset)
    return (header + extra_header + encoded).ljust(length, b'\0')


def buffer(*records, next_usn=1000):
    return struct.pack('<q', next_usn) + b''.join(records)


class FakeBackend:
    def __init__(self, records=(), *, info=None, paths=None):
        self.info = info or JournalInfo(123, 10, 1000, 10)
        self.records = records
        self.paths = paths or {'0000000000000007': 'C:\\docs'}
        self.reads = []
        self.resolutions = []
        self.closed = False

    def query(self):
        return self.info

    def read(self, start_usn, journal_id, max_bytes):
        self.reads.append((start_usn, journal_id, max_bytes))
        selected = [r for r in self.records if parse_usn_buffer(buffer(r)).records[0].usn >= start_usn]
        return buffer(*selected, next_usn=self.info.next_usn)

    def resolve(self, file_id):
        self.resolutions.append(file_id)
        if file_id not in self.paths:
            raise PermissionError('synthetic denial')
        return self.paths[file_id]

    def close(self):
        self.closed = True


CURSOR = {'journal_id': '123', 'next_usn': 100}


@pytest.mark.parametrize('version', [2, 3])
def test_record_layout_unicode_and_runtime_name_offset(version):
    result = parse_usn_buffer(buffer(record('分析😀.txt', version=version, extra_header=b'\0' * 8)))
    found = result.records[0]
    assert found.name == '分析😀.txt'
    assert found.usn == 100
    assert found.reason == FILE_CREATE
    assert len(found.file_id) == (16 if version == 2 else 32)
    assert result.next_usn == 1000
    assert not result.has_more


def test_empty_read_uses_kernel_cursor():
    assert parse_usn_buffer(buffer(next_usn=100)).records == []
    backend = FakeBackend(info=JournalInfo(123, 0, 100))
    result = VolumeJournal('C:\\', backend).poll(CURSOR)
    assert result['available']
    assert result['next_cursor'] == CURSOR
    assert not result['has_more']
    assert not result['reconciliation_required']


def test_bounded_poll_restarts_at_first_unconsumed_record_and_counts_filtered_records():
    backend = FakeBackend([record(usn=u) for u in (100, 200, 300)])
    journal = VolumeJournal('C:\\', backend)
    first = journal.poll(CURSOR, max_events=2, allowed=lambda p: False)
    assert first['events'] == []
    assert first['records_examined'] == 2
    assert first['next_cursor']['next_usn'] == 300
    assert first['has_more']
    # A process restart needs only the durable cursor and persisted event queue.
    restarted = VolumeJournal('C:\\', backend).poll(first['next_cursor'])
    assert [r['usn'] for r in restarted['events']] == [300]
    assert restarted['next_cursor']['next_usn'] == 1000
    assert not restarted['has_more']
    assert backend.reads == [(100, 123, 262144), (300, 123, 262144)]
    assert len(backend.resolutions) == 2  # One parent resolution per poll.


@pytest.mark.parametrize(('cursor', 'reason'), [
    (None, 'initial_baseline_required'),
    ({'journal_id': 'old', 'next_usn': 100}, 'journal_reset'),
    ({'journal_id': '123', 'next_usn': 1}, 'journal_entries_lost'),
    ({'journal_id': '123', 'next_usn': 1100}, 'cursor_ahead_of_journal'),
    ({'journal_id': 123, 'next_usn': 100}, 'invalid_cursor'),
    ({'journal_id': '123', 'next_usn': True}, 'invalid_cursor'),
])
def test_baseline_anchor_does_not_read_historical_disk_records(cursor, reason):
    backend = FakeBackend()
    result = VolumeJournal('c:/', backend).poll(cursor)
    assert result['available']
    assert result['volume'] == 'C:\\'
    assert result['reason'] == reason
    assert result['reconciliation_required']
    assert result['next_cursor'] == {'journal_id': '123', 'next_usn': 1000}
    assert backend.reads == []


def test_lowest_valid_usn_takes_precedence_over_oldest_stored_record():
    backend = FakeBackend(info=JournalInfo(123, 10, 1000, 500))
    result = VolumeJournal('C:\\', backend).poll(CURSOR)
    assert result['reason'] == 'journal_entries_lost'


def test_file_rename_and_delete_use_record_parent_and_name_not_current_file_path():
    backend = FakeBackend([
        record('old.txt', usn=100, reason=RENAME_OLD_NAME),
        record('new.txt', usn=200, reason=RENAME_NEW_NAME),
        record('deleted.txt', usn=300, reason=FILE_DELETE),
    ])
    result = VolumeJournal('C:\\', backend).poll(CURSOR)
    assert [(e['action'], e['path']) for e in result['events']] == [
        ('delete', 'C:\\docs\\old.txt'), ('upsert', 'C:\\docs\\new.txt'),
        ('delete', 'C:\\docs\\deleted.txt'),
    ]
    assert not result['reconciliation_required']
    assert backend.resolutions == ['0000000000000007']


@pytest.mark.parametrize(('reason', 'attributes'), [
    (RENAME_OLD_NAME, DIRECTORY), (FILE_DELETE, DIRECTORY),
    (HARD_LINK_CHANGE, 0), (REPARSE_POINT_CHANGE, 0),
    (RENAME_OLD_NAME | RENAME_NEW_NAME, 0),
])
def test_structural_changes_require_reconciliation(reason, attributes):
    result = VolumeJournal('C:\\', FakeBackend([record(reason=reason, attributes=attributes)])).poll(CURSOR)
    assert result['reconciliation_required']
    assert result['events'][0]['action'] == 'reconcile'
    assert result['events'][0]['is_directory'] == bool(attributes & DIRECTORY)


def test_unresolved_parent_does_not_guess_or_leak_a_path_and_diagnostics_are_bounded():
    backend = FakeBackend([record(usn=100 + i, parent_id=200 + i) for i in range(30)])
    result = VolumeJournal('C:\\', backend).poll(CURSOR)
    assert result['events'] == []
    assert result['reconciliation_required']
    assert result['reason'] == 'unresolved_changes'
    assert len(result['diagnostics']) == 8
    assert result['diagnostics'][0] == {'reason': 'parent_unresolved', 'usn': 100}
    assert result['next_cursor']['next_usn'] == 1000


def test_allowed_receives_path_and_excluded_changes_do_not_force_reconciliation():
    observed = []
    def allowed(path):
        observed.append(path)
        return False
    result = VolumeJournal('C:\\', FakeBackend([record(attributes=DIRECTORY)])).poll(CURSOR, allowed=allowed)
    assert len(observed) == 1 and isinstance(observed[0], Path)
    assert not result['events']
    assert not result['reconciliation_required']


@pytest.mark.parametrize('name', ['..', '.', '../escape', 'bad\\name', 'x:stream', '\0x', 'tail.', 'tail ',
                                 'CON', 'NUL.txt', 'COM1.log', 'LPT³', 'bad?name'])
def test_unsafe_names_request_reconciliation(name):
    result = VolumeJournal('C:\\', FakeBackend([record(name)])).poll(CURSOR)
    assert result['events'] == []
    assert result['reconciliation_required']
    assert result['diagnostics'][0]['reason'] == 'unsafe_record_name'


@pytest.mark.parametrize('parent', ['D:\\other', '\\\\server\\share', '\\\\?\\UNC\\server\\share', 'relative'])
def test_parent_resolution_cannot_escape_volume(parent):
    with pytest.raises(JournalError, match='parent_outside_volume'):
        _record_path('C:\\', parent, 'a.txt')


def test_extended_local_path_is_normalized_for_scope_callback():
    assert _record_path('C:\\', '\\\\?\\C:\\docs', 'a.txt') == 'C:\\docs\\a.txt'


@pytest.mark.parametrize('raw', [
    b'', b'1234567', struct.pack('<q', -1), buffer(record()[:-1]),
    buffer(record(usn=200), record(usn=100)), buffer(record(usn=100), next_usn=100),
    buffer(record()) + b'partial',
])
def test_malformed_records_are_never_silently_skipped(raw):
    with pytest.raises(JournalError):
        parse_usn_buffer(raw)


def test_unsupported_major_version_falls_back_without_advancing_cursor():
    raw = bytearray(buffer(record()))
    struct.pack_into('<H', raw, 12, 4)
    backend = FakeBackend()
    backend.read = lambda *args: bytes(raw)
    result = VolumeJournal('C:\\', backend).poll(CURSOR)
    assert not result['available']
    assert result['reason'] == 'unsupported_record_version'
    assert result['next_cursor'] == CURSOR
    assert result['reconciliation_required']


def test_journal_reset_racing_query_and_read_reanchors_for_reconciliation():
    backend = FakeBackend()
    def reset_during_read(*args):
        backend.info = JournalInfo(456, 0, 500)
        raise JournalError('journal_entries_lost', 1181)
    backend.read = reset_during_read
    result = VolumeJournal('C:\\', backend).poll(CURSOR)
    assert result['available']
    assert result['reconciliation_required']
    assert result['reason'] == 'journal_reset_during_read'
    assert result['next_cursor'] == {'journal_id': '456', 'next_usn': 500}


@pytest.mark.parametrize('reason', ['access_denied', 'filesystem_not_ntfs', 'journal_not_active'])
def test_unavailable_backend_is_explicit_and_keeps_cursor(reason):
    backend = FakeBackend()
    def unavailable():
        raise JournalError(reason)
    backend.query = unavailable
    result = VolumeJournal('C:\\', backend).poll(CURSOR)
    assert not result['available']
    assert result['reason'] == reason
    assert result['next_cursor'] == CURSOR
    assert result['reconciliation_required']


@pytest.mark.parametrize('root', ['C:', 'C:\\selected', '\\\\server\\share\\', '/'])
def test_only_drive_roots_enable_backend(root):
    result = VolumeJournal(root, FakeBackend()).poll()
    assert not result['available']
    assert result['reason'] == 'drive_root_required'


def test_close_can_be_called_repeatedly():
    backend = FakeBackend()
    journal = VolumeJournal('C:\\', backend)
    journal.close()
    journal.close()
    assert backend.closed


@pytest.mark.parametrize('options', [{'max_events': 0}, {'max_events': 4097}, {'max_events': True},
                                     {'max_bytes': 1024}, {'max_bytes': 1048577}])
def test_poll_rejects_unbounded_or_invalid_work_budgets(options):
    with pytest.raises(ValueError):
        VolumeJournal('C:\\', FakeBackend()).poll(CURSOR, **options)


def test_native_read_request_is_nonwaiting_and_limits_versions_to_supported_layouts():
    backend = WindowsJournalBackend.__new__(WindowsJournalBackend)
    calls = []
    backend._ioctl = lambda *args: calls.append(args) or b''
    backend.read(100, 123, 65536)
    code, request, budget = calls[0]
    assert code == 0x000900BB
    assert len(request) == 48
    start, mask, close_only, timeout, wait_bytes, journal_id, minimum, maximum = struct.unpack('<qIIQQQHH4x', request)
    assert (start, close_only, timeout, wait_bytes, journal_id, minimum, maximum) == (100, 0, 0, 0, 123, 2, 3)
    assert not mask & 0x80000000  # Close-only noise does not consume poll budget.
    assert mask & FILE_CREATE and mask & FILE_DELETE and mask & RENAME_NEW_NAME
    assert budget == 65536


@pytest.mark.parametrize('file_id', ['0123456789abcdef', '0102030405060708090a0b0c0d0e0f10'])
def test_native_id_resolution_uses_full_width_handle_and_closes_it(file_id):
    import ctypes
    from types import SimpleNamespace
    backend = WindowsJournalBackend.__new__(WindowsJournalBackend)
    backend._open = lambda: None
    backend.handle = 0x100000001
    opened, closed = [], []
    def open_file(volume, descriptor, access, share, security, flags):
        opened.append((volume, descriptor.raw[:24], access, share, flags))
        return 0x200000001
    def path_name(handle, result, size, flags):
        assert handle == 0x200000001
        result.value = '\\\\?\\C:\\docs'
        return len(result.value)
    backend.kernel = SimpleNamespace(OpenFileById=open_file, GetFinalPathNameByHandleW=path_name,
                                     CloseHandle=closed.append)
    assert backend.resolve(file_id) == '\\\\?\\C:\\docs'
    volume, descriptor, access, share, flags = opened[0]
    assert volume == 0x100000001
    assert struct.unpack_from('<II', descriptor) == (24, 0 if len(file_id) == 16 else 2)
    assert access == 0x80 and share == 7
    assert flags == 0x02000000 | 0x00200000
    expected_id = struct.pack('<Q', int(file_id, 16)) if len(file_id) == 16 else bytes.fromhex(file_id)
    assert descriptor[8:8 + len(expected_id)] == expected_id
    assert closed == [0x200000001]


def test_native_query_validates_v0_prefix_without_requiring_exact_output_size():
    backend = WindowsJournalBackend.__new__(WindowsJournalBackend)
    backend._ioctl = lambda *args: struct.pack('<QqqqqQQ', 123, 10, 1000, 20, 1000000, 100000, 10000) + b'new fields'
    assert backend.query() == JournalInfo(123, 10, 1000, 20)


@pytest.mark.skipif(os.name == 'nt', reason='Non-Windows fallback only')
def test_platform_fallback_needs_no_native_libraries():
    assert VolumeJournal('C:\\').poll()['reason'] == 'platform_unsupported'
