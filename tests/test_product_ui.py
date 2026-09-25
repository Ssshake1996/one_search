import copy
import json
import os
from pathlib import Path
import threading
import time

import pytest

from data_search.config import atomic_json, defaults
from data_search.product_ui import add_product_panels


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Tk workflow acceptance uses the validated Windows host')


@pytest.fixture
def panel(tmp_path):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.attributes('-alpha', 0.0)
    root.geometry('900x700')
    notebook = ttk.Notebook(root)
    notebook.pack(fill='both', expand=True)
    config = defaults(str(tmp_path/'data'), [str(tmp_path)])
    path = tmp_path/'config.json'
    atomic_json(path, config)
    pages = add_product_panels(notebook, path)
    root.update()
    controller = notebook.one_search_product_controller
    yield root, notebook, controller, path, pages
    if root.winfo_exists():
        root.destroy()


def wait(root, panel, timeout=8):
    deadline = time.monotonic()+timeout
    root.update()
    while panel.busy and time.monotonic()<deadline:
        root.update()
        time.sleep(.01)
    root.update()
    assert not panel.busy


def hit(identity, path):
    return {'id': identity, 'document_id': 'd:'+identity.split(':')[1], 'source_id': 'files',
        'name': Path(path).name, 'path': str(path), 'status': 'ready', 'stale': False,
        'citation': {'id': identity, 'path': str(path), 'locator': {'line': 1}, 'stale': False}}


def test_search_context_citation_diagnosis_refresh_uses_real_engine(panel, monkeypatch, tmp_path):
    from data_search.engine import Engine
    root, notebook, ui, config_path, _ = panel
    source = tmp_path/'资料'
    source.mkdir()
    document = source/'检索笔记.txt'
    document.write_text('one_search userneedle evidence', encoding='utf-8')
    config = defaults(str(tmp_path/'real-data'), [str(source)])
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    atomic_json(config_path, config)
    engine = Engine(config)
    try:
        engine.scan_once()
        monkeypatch.setattr('data_search.service.rpc', lambda _config, method, parameters: engine.dispatch(method, parameters))
        # add_product_panels imports rpc once; create the tested controller after patching.
        notebook.destroy()
        from tkinter import ttk
        notebook = ttk.Notebook(root)
        notebook.pack(fill='both', expand=True)
        add_product_panels(notebook, config_path)
        ui = notebook.one_search_product_controller
        ui.text.set('userneedle')
        ui.mode.set('keyword')
        ui.search()
        wait(root, ui)
        identity = ui.results.get_children()[0]
        ui.results.selection_set(identity)
        wait(root, ui)
        assert 'userneedle' in ui.preview.get('1.0', 'end')
        ui.copy_citation()
        citation = json.loads(root.clipboard_get())
        assert citation['path'] == str(document) and not citation['stale']
        manual = source/'尚未存在的资料.txt'
        ui.target_path.set(str(manual))
        ui.results.selection_set(identity)
        wait(root, ui)
        assert ui.target_path.get() == str(manual)
        ui.target('diagnose_path')
        wait(root, ui)
        assert 'missing_or_moved' in ui.preview.get('1.0', 'end')
        ui.use_selected_path()
        ui.target('diagnose_path')
        wait(root, ui)
        assert 'ready' in ui.preview.get('1.0', 'end')
        document.write_text('updated userneedle evidence', encoding='utf-8')
        ui.target('refresh_path')
        wait(root, ui)
        assert '重新查找' in ui.results.item(identity, 'values')[1]
        assert 'refreshed' in ui.preview.get('1.0', 'end')
        ui.search()
        wait(root, ui)
        ui.results.selection_set(ui.results.get_children()[0])
        wait(root, ui)
        assert 'updated userneedle' in ui.preview.get('1.0', 'end')
    finally:
        engine.close()


def test_fast_selection_never_shows_previous_hit_under_new_selection(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    started, release = threading.Event(), threading.Event()
    first, second = hit('c:1', tmp_path/'A.txt'), hit('c:2', tmp_path/'B.txt')
    calls = []
    def rpc(config, method, parameters):
        calls.append((method, parameters))
        if method == 'search':
            return {'results': [first, second]}
        if parameters['id'] == 'c:1':
            started.set()
            assert release.wait(3)
        item = first if parameters['id'] == 'c:1' else second
        return {'document': item, 'chunks': [{'locator': {}, 'text': item['name']}], 'citation': item['citation']}
    monkeypatch.setattr('data_search.service.rpc', rpc)
    root = tk.Tk(); root.withdraw()
    notebook = ttk.Notebook(root); notebook.pack(fill='both', expand=True)
    config_path = tmp_path/'config.json'; atomic_json(config_path, defaults(str(tmp_path/'data'), []))
    add_product_panels(notebook, config_path)
    ui = notebook.one_search_product_controller
    try:
        ui.text.set('query'); ui.search(); wait(root, ui)
        ui.results.selection_set('c:1'); root.update()
        assert started.wait(1)
        ui.results.selection_set('c:2'); root.update()
        release.set(); wait(root, ui)
        assert 'B.txt' in ui.preview.get('1.0', 'end') and 'A.txt' not in ui.preview.get('1.0', 'end')
        ui.copy_citation()
        assert json.loads(root.clipboard_get())['id'] == 'c:2'
        assert [parameters['id'] for method, parameters in calls if method == 'read_context'] == ['c:1', 'c:2']
    finally:
        release.set(); root.destroy()


def test_manual_path_is_never_overridden_by_old_selection(panel, monkeypatch, tmp_path):
    root, notebook, ui, config_path, _ = panel
    calls = []
    # Exercise actual captured action functions via a fresh controller.
    monkeypatch.setattr('data_search.service.rpc', lambda c, method, args: calls.append((method, args)) or {'code': 'ready'})
    notebook.destroy()
    from tkinter import ttk
    notebook = ttk.Notebook(root); notebook.pack(fill='both', expand=True)
    add_product_panels(notebook, config_path)
    ui = notebook.one_search_product_controller
    target = tmp_path/'explicit-target.txt'
    ui.target_path.set(str(target))
    ui.text.set(str(tmp_path/'different-query.txt'))
    ui.target('diagnose_path'); wait(root, ui)
    assert calls[-1] == ('diagnose_path', {'path': str(target)})
    ui.target_path.set('relative.txt'); ui.target('refresh_path')
    assert len(calls) == 1 and '完整' in ui.status.get()


def test_maintenance_captures_tk_values_before_worker_and_coordinates_busy(panel, monkeypatch):
    import tkinter as tk
    root, notebook, ui, _, _ = panel
    main = threading.get_ident()
    original_get = tk.StringVar.get
    def guarded_get(variable):
        assert threading.get_ident() == main, 'Tk variable accessed in a worker'
        return original_get(variable)
    monkeypatch.setattr(tk.StringVar, 'get', guarded_get)
    captured = []
    def space(config, install_dir=None):
        captured.append(install_dir)
        return {'retained_backups': [{'id': 'verified-backup', 'cleanup_eligible': True}]}
    monkeypatch.setattr('data_search.maintenance.compatibility_info', lambda c: {'version': 'test'})
    monkeypatch.setattr('data_search.maintenance.space_report', space)
    monkeypatch.setattr('data_search.maintenance.cleanup_backup', lambda c, identifier, install_dir=None: captured.append((identifier, install_dir)) or {'deleted': True})
    ui.install.set('captured-install')
    ui.overview(); wait(root, ui)
    ui.backup_id.set('verified-backup'); ui.cleanup(); wait(root, ui)
    assert captured == ['captured-install', ('verified-backup', 'captured-install')]
    notebook.one_search_main_busy = lambda: True
    ui.overview()
    assert not ui.busy and len(captured) == 2


def test_restore_applies_reviewed_snapshot_and_invalidates_old_settings(panel, monkeypatch, tmp_path):
    root, notebook, ui, config_path, _ = panel
    export = tmp_path/'restore.json'
    export.write_text(json.dumps({'reviewed': 1}))
    monkeypatch.setattr('tkinter.filedialog.askopenfilename', lambda **kwargs: str(export))
    applied = []
    def restore(config, bundle, apply=False):
        if apply: applied.append(copy.deepcopy(bundle))
        return {'applied': apply, 'issues': []}
    monkeypatch.setattr('data_search.maintenance.restore_config', restore)
    ui.restore(); wait(root, ui)
    export.write_text(json.dumps({'unreviewed': 2}))
    ui.apply_restore(); wait(root, ui)
    assert applied == [{'reviewed': 1}] and notebook.one_search_config_changed
    ui.restore(); wait(root, ui)
    config = json.loads(config_path.read_text()); config['scan_interval_seconds'] += 1; atomic_json(config_path, config)
    ui.apply_restore(); wait(root, ui)
    assert len(applied) == 1 and '重新预览' in ui.status.get()


def test_close_and_cancellation_do_not_interrupt_mutation_or_leave_tk_callbacks(panel):
    root, notebook, ui, _, _ = panel
    release = threading.Event()
    completed = []
    ui.run(lambda: release.wait(3) or {}, completed.append, mutation=True)
    assert not ui.request_close()
    ui.cancel()
    assert not ui.cancelled
    release.set(); wait(root, ui)
    assert completed == [True] and ui.request_close()
    release.clear()
    ui.run(lambda: release.wait(3) or {}, completed.append)
    ui.cancel(); release.set(); wait(root, ui)
    assert completed == [True] and ui.request_close()
    notebook.destroy(); root.update()
    assert ui.closed and ui.after_id is None


@pytest.mark.parametrize('size', ['900x700', '740x560'])
def test_both_panels_remain_scrollable_within_window_width(panel, size):
    root, notebook, ui, _, pages = panel
    root.geometry(size)
    for page, canvas in zip(pages, [ui.search_canvas, ui.maintenance_canvas]):
        notebook.select(page); root.update()
        region = tuple(map(float, canvas.cget('scrollregion').split()))
        assert region[2] <= canvas.winfo_width()+1
        assert canvas.winfo_width() > 600 and region[3] > 300
        canvas.yview_moveto(1); root.update()
        assert canvas.yview()[1] >= .99
