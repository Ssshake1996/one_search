"""Native settings must not overwrite newer DSH Web settings."""
import gc
import json
import time

import pytest

from data_search import service, setup_ui
from data_search.config import atomic_json, defaults


def descendants(window):
    pending, result = [window], []
    while pending:
        widget = pending.pop()
        result.append(widget)
        pending.extend(widget.winfo_children())
    return result


def save_and_wait(window):
    widgets = descendants(window)
    button = next(widget for widget in widgets if 'text' in widget.keys()
                  and widget.cget('text') == '保存并启动')
    tabs = next(widget for widget in widgets if hasattr(widget, 'one_search_main_busy'))
    button.invoke()
    deadline = time.monotonic() + 5
    while tabs.one_search_main_busy() and time.monotonic() < deadline:
        window.update()
        time.sleep(.01)
    assert not tabs.one_search_main_busy(), 'Settings operation did not finish'


@pytest.fixture
def editor(tmp_path, monkeypatch, tk_root):
    import tkinter as tk
    root = tmp_path / 'synthetic docs'
    root.mkdir()
    path = tmp_path / 'config.json'
    original = defaults(str(tmp_path / 'data'), [str(root)])
    original['semantic']['enabled'] = False
    calls, errors = [], []
    window = tk.Toplevel(tk_root)
    window.withdraw()
    monkeypatch.setattr(tk, 'Tk', lambda: window)
    monkeypatch.setattr('tkinter.messagebox.showerror', lambda *a, **k: errors.append(a))
    monkeypatch.setattr(service, 'stop_service', lambda config: calls.append('stop'))
    monkeypatch.setattr(service, 'start_service', lambda config: calls.append('start') or {'status': 'running'})
    yield window, path, original, calls, errors
    window.destroy()
    gc.collect()


@pytest.mark.parametrize('already_exists', [True, False])
def test_stale_native_form_rejects_newer_config_and_newly_created_path(editor, monkeypatch, already_exists):
    window, path, original, calls, errors = editor
    if already_exists:
        atomic_json(path, original)

    def exercise():
        external = dict(original, exclude_names=['saved-by-web-after-native-opened'])
        atomic_json(path, external)
        expected = path.read_bytes()
        save_and_wait(window)
        assert not errors and not calls
        assert path.read_bytes() == expected
        details = [widget.get('1.0', 'end') for widget in descendants(window)
                   if widget.winfo_class() == 'Text']
        assert any('SettingsConflict' in text for text in details)

    monkeypatch.setattr(window, 'mainloop', exercise)
    assert setup_ui.main(['--config', str(path)]) == 0


def test_native_first_creation_and_second_save_update_revision(editor, monkeypatch):
    window, path, _, calls, errors = editor

    def exercise():
        assert not path.exists()
        save_and_wait(window)
        assert path.exists() and calls == ['start'] and not errors
        first = setup_ui.settings_revision(path)
        save_and_wait(window)
        assert calls == ['start', 'stop', 'start'] and not errors
        assert setup_ui.settings_revision(path) == first

    monkeypatch.setattr(window, 'mainloop', exercise)
    assert setup_ui.main(['--config', str(path)]) == 0


def test_revision_after_native_save_is_the_locked_revision_not_later_writer(editor, monkeypatch):
    window, path, original, calls, errors = editor
    atomic_json(path, original)
    apply = setup_ui.activate_settings
    saved_revisions = []

    def race_after_apply(*args, **kwargs):
        result = apply(*args, **kwargs)
        saved_revisions.append(result['settings_revision'])
        external = json.loads(path.read_text(encoding='utf-8'))
        external['exclude_names'].append('web-writer-after-native-commit')
        atomic_json(path, external)
        return result

    monkeypatch.setattr(setup_ui, 'activate_settings', race_after_apply)

    def exercise():
        save_and_wait(window)
        assert calls == ['stop', 'start'] and not errors
        assert saved_revisions[0] != setup_ui.settings_revision(path)
        expected = path.read_bytes()
        save_and_wait(window)
        assert calls == ['stop', 'start']
        assert path.read_bytes() == expected

    monkeypatch.setattr(window, 'mainloop', exercise)
    assert setup_ui.main(['--config', str(path)]) == 0
