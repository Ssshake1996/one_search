import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from data_search import cli, scope
from data_search.config import defaults, load_config
from data_search.engine import Engine


def configuration(tmp_path, roots=None):
    config = defaults(str(tmp_path / 'index'), roots)
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    return config


def test_init_defaults_machine_and_preserves_explicit_roots(tmp_path, capsys):
    path = tmp_path / 'config.json'
    excluded = tmp_path / 'private'
    assert cli.main(['init', '--config', str(path), '--data-dir', str(tmp_path / 'index'),
                     '--exclude', str(excluded)]) == 0
    loaded = load_config(path)
    assert loaded['scope'] == 'machine' and loaded['roots'] == []
    assert loaded['exclude_paths'] == [str(excluded.resolve())]
    assert json.loads(capsys.readouterr().out)['scope'] == 'machine'
    original = path.read_bytes()
    assert cli.main(['init', '--config', str(path), '--data-dir', str(tmp_path / 'other')]) == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize('empty', [False, True])
def test_legacy_config_never_broadens_scope(tmp_path, empty):
    config = configuration(tmp_path, [] if empty else [str(tmp_path / 'selected')])
    del config['scope']
    del config['indexing']
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    loaded = load_config(path)
    assert loaded['scope'] == 'directories'
    assert loaded['roots'] == config['roots']
    assert loaded['indexing']['content_scope'] == 'all'


@pytest.mark.parametrize('machine', [False, True])
def test_two_independent_roots_and_exclusions(tmp_path, monkeypatch, machine):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    excluded = second / 'private'
    excluded.mkdir()
    (first / 'alpha.txt').write_text('scopealphaneedle')
    (second / 'beta.txt').write_text('scopebetaneedle')
    (excluded / 'private.txt').write_text('scopecachedprivate')
    ignored = first / '.GIT'
    ignored.mkdir()
    (ignored / 'hidden.txt').write_text('scopeexcludedname')
    roots = [str(first), str(second)]
    monkeypatch.setattr(scope, 'discover_volumes', lambda: (roots, []))
    config = configuration(tmp_path, None if machine else roots)
    config['exclude_paths'] = [str(excluded)]
    engine = Engine(config)
    try:
        status = engine.scan_once()
        assert not status['last_error']
        assert engine.search('scopealphaneedle', 'keyword')['results']
        assert engine.search('scopebetaneedle', 'keyword')['results']
        assert not engine.search('scopecachedprivate', 'keyword')['results']
        assert not engine.search('scopeexcludedname', 'keyword')['results']
        assert set(status['file_scope']['effective_roots']) == set(roots)
    finally:
        engine.close()


def test_machine_disk_refresh_and_scope_narrowing_purge_cached_text(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    (first / 'alpha.txt').write_text('persistentneedle')
    (second / 'beta.txt').write_text('revokedneedle')
    roots = [str(first), str(second)]
    monkeypatch.setattr(scope, 'discover_volumes', lambda: (roots.copy(), []))
    config = configuration(tmp_path)
    engine = Engine(config)
    try:
        engine.scan_once()
        assert engine.search('revokedneedle', 'keyword')['results']
        roots.remove(str(second))
        engine.scan_once()
        assert not engine.search('revokedneedle', 'keyword')['results']
        assert not engine.store.rows("SELECT id FROM documents WHERE path=?", (str(second / 'beta.txt'),))
    finally:
        engine.close()
    config['scope'] = 'directories'
    config['roots'] = [str(second)]
    engine = Engine(config)
    try:
        assert not engine.store.rows('SELECT * FROM chunks')
        engine.scan_once()
        assert engine.search('revokedneedle', 'keyword')['results']
    finally:
        engine.close()


def test_missing_roots_and_bounded_walk_errors_are_visible(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    missing = tmp_path / 'missing'
    monkeypatch.setattr(scope, 'discover_volumes', lambda: ([str(root), str(missing)], []))
    for index in range(100):
        (root / str(index)).mkdir()
    real_scandir = scope.os.scandir
    def failed_scandir(base):
        if Path(base).parent == root:
            raise PermissionError(13,'access denied',str(base))
        return real_scandir(base)
    monkeypatch.setattr('data_search.catalog.os.scandir', failed_scandir)
    engine = Engine(configuration(tmp_path))
    try:
        for _ in range(5):
            status = engine.scan_once(full=not engine.catalog.active)
            if not engine.catalog.active:
                break
        report = status['file_scope']
        assert report['unavailable_roots'][0]['path'] == str(missing)
        assert report['scan_errors']['count'] == 100
        assert len(report['scan_errors']['samples']) == 20
        assert status['coverage']['source_errors'][str(root)] == 'scan_incomplete'
    finally:
        engine.close()


def test_machine_uses_periodic_scans_without_recursive_watchers(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    monkeypatch.setattr(scope, 'discover_volumes', lambda: ([str(root)], []))
    def disallowed_observer():
        pytest.fail('machine scope must not allocate recursive filesystem watchers')
    monkeypatch.setattr('watchdog.observers.Observer', disallowed_observer)
    config = configuration(tmp_path)
    config['scan_interval_seconds'] = 1
    engine = Engine(config)
    calls = []
    monkeypatch.setattr(engine, 'scan_once', lambda full=True: calls.append(full))
    try:
        engine.start_background()
        deadline = time.monotonic() + 4
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(.05)
        assert calls[:2] == [True, True]
        assert engine.status()['file_scope']['monitoring'] == 'periodic'
    finally:
        engine.close()


def test_windows_discovery_only_fixed_disks(monkeypatch):
    kernel = SimpleNamespace(GetLogicalDrives=lambda: (1 << 2) | (1 << 3) | (1 << 17),
                             GetDriveTypeW=lambda value: {'C': 3, 'D': 2, 'R': 4}[value.value[0]])
    monkeypatch.setattr(scope.ctypes, 'windll', SimpleNamespace(kernel32=kernel), raising=False)
    roots, skipped = scope._windows_volumes()
    assert roots == ['C:\\']
    assert {item['path'] for item in skipped} == {'D:\\', 'R:\\'}


def test_linux_discovery_local_mounts_and_remote_virtual_exclusions(monkeypatch):
    mounts = [('/', 'ext4'), ('/home', 'xfs'), ('/proc', 'proc'), ('/run', 'tmpfs'),
              ('/mnt/shared', 'nfs4'), ('/mnt/smb', 'cifs'), ('/mnt/ssh', 'fuse.sshfs')]
    monkeypatch.setattr(scope.psutil, 'disk_partitions', lambda all: [SimpleNamespace(mountpoint=p, fstype=f) for p, f in mounts])
    roots, skipped = scope._linux_volumes()
    assert roots == ['/', '/home']
    assert {item['path'] for item in skipped} == {p for p, _ in mounts[2:]}


def test_discovery_failure_does_not_fall_back_to_whole_root(tmp_path, monkeypatch):
    def unavailable():
        raise OSError('discovery_unavailable')
    monkeypatch.setattr(scope, 'discover_volumes', unavailable)
    engine = Engine(configuration(tmp_path))
    try:
        status = engine.scan_once()
        assert status['file_scope']['effective_roots'] == []
        assert status['coverage']['source_errors']['file_scope'] == 'discovery_unavailable'
    finally:
        engine.close()


def test_skipped_remote_mount_is_rejected_without_resolving_it(tmp_path, monkeypatch):
    root, remote = tmp_path / 'disk', tmp_path / 'disk' / 'remote'
    root.mkdir()
    monkeypatch.setattr(scope, 'discover_volumes', lambda: ([str(root)], [{'path':str(remote), 'reason':'remote_filesystem'}]))
    resolve = Path.resolve
    def guarded_resolve(path, *args, **kwargs):
        if path.is_relative_to(remote):
            pytest.fail('excluded remote mount must not be resolved or inspected')
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', guarded_resolve)
    files = scope.FileScope(configuration(tmp_path))
    assert not files.allowed(remote / 'file.txt')
    assert files.allowed(root / 'local.txt')


def test_link_directory_is_not_descended(tmp_path, monkeypatch):
    root, linked = tmp_path / 'root', tmp_path / 'root' / 'junction'
    linked.mkdir(parents=True)
    (linked / 'hidden.txt').write_text('junctionneedle')
    (root / 'visible.txt').write_text('visibleneedle')
    real_check = scope.link_directory
    monkeypatch.setattr('data_search.engine.link_directory', lambda path: path == linked or real_check(path))
    monkeypatch.setattr('data_search.catalog.link_directory', lambda path: path == linked or real_check(path))
    engine = Engine(configuration(tmp_path, [str(root)]))
    try:
        engine.scan_once()
        assert engine.search('visibleneedle', 'keyword')['results']
        assert not engine.search('junctionneedle', 'keyword')['results']
    finally:
        engine.close()


def test_source_checkout_examples_and_demo_remain_searchable(tmp_path, monkeypatch):
    checkout = tmp_path / 'checkout'
    package = checkout / 'src' / 'data_search'
    package.mkdir(parents=True)
    monkeypatch.setattr(scope, '__file__', str(package / 'scope.py'))
    config = configuration(tmp_path, [str(checkout)])
    files = scope.FileScope(config)
    assert files.allowed(checkout / 'examples' / 'sample-documents' / 'example.txt')
    assert files.allowed(checkout / 'docs' / 'README.md')
    assert files.allowed(checkout / '.runtime' / 'demo' / 'files' / 'example.txt')
    assert not files.allowed(package / 'engine.py')


@pytest.mark.parametrize('update', [
    {'scope': 'unknown'}, {'scope': 'machine', 'roots': ['some-directory']},
    {'exclude_paths': 'not-a-list'}, {'indexing': {'content_scope': 'unknown'}},
    {'indexing': {'semantic_extensions': ['txt']}},
])
def test_invalid_scope_config_rejected(tmp_path, update):
    config = configuration(tmp_path, [])
    config.update(update)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_config(path)
