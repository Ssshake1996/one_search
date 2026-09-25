from copy import deepcopy
import os
import sqlite3
import time

import pytest

from data_search.database_ui import connection_candidate, editor_selection, open_database_editor


@pytest.fixture(scope="module")
def database_ui_root(tk_root):
    tk_root.withdraw()
    return tk_root


def test_connection_form_preserves_advanced_options_but_never_plaintext_password():
    original = {"id": "orders", "kind": "postgres", "host": "old", "database": "business", "user": "reader",
        "password_env": "OLD_VARIABLE", "ssl": {"sslcert": "client.pem", "sslkey": "key.pem", "sslmode": "require"},
        "sync": {"page_size": 50}, "allowed_tables": ["public.orders"]}
    before = deepcopy(original)
    ref = "one-search-vault:windows:" + "0" * 32
    result = connection_candidate(original, {"id": "orders", "kind": "postgres", "host": "new",
        "database": "business", "user": "reader", "auth": "vault", "password": "NEVER_STORE",
        "ssl_mode": "verify-full", "ssl_ca": "ca.pem"}, ref)
    assert result["credential_ref"] == ref and "password_env" not in result and "password" not in result
    assert result["ssl"] == {"sslcert": "client.pem", "sslkey": "key.pem", "sslmode": "verify-full", "sslrootcert": "ca.pem"}
    assert result["sync"]["page_size"] == 50 and original == before


def test_changing_database_kind_does_not_reuse_old_engine_auth_or_schema():
    original = {"id": "old", "kind": "postgres", "allowed_tables": ["public.orders"], "credential_ref": "old", "ssl": {"sslmode": "require"}}
    result = connection_candidate(original, {"id": "new", "kind": "sqlite", "path": "synthetic.sqlite"})
    assert result["allowed_tables"] == [] and "credential_ref" not in result and "ssl" not in result


def test_editor_never_implicitly_grants_columns_or_watermark_maintenance():
    state = {"columns": {"id", "body", "updated_at"}, "text": {"body"}, "index": True,
        "key": "id", "watermark": "updated_at", "watermark_confirmed": False}
    with pytest.raises(ValueError, match="水位"):
        editor_selection("notes", state)
    state["watermark_confirmed"] = True
    assert editor_selection("notes", state)["updated_column"] == "updated_at"
    state.update(index=False, text=set())
    assert "updated_column" not in editor_selection("notes", state)
    state["columns"].clear()
    with pytest.raises(ValueError, match="至少一个"):
        editor_selection("notes", state)


@pytest.mark.skipif(os.name != "nt", reason="Real Tk dialog smoke runs on the validated Windows host")
def test_real_tk_discovery_field_selection_preflight_and_callback(tmp_path, database_ui_root):
    path = tmp_path / "资料.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT, secret TEXT); INSERT INTO notes VALUES(1,'synthetic','excluded');")
    root = database_ui_root
    saved = []
    editor = open_database_editor(root, {}, saved.append)
    editor.window.withdraw()
    def wait():
        deadline = time.monotonic() + 10
        while editor.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        root.update()
        assert not editor.busy
    try:
        editor.values["id"].set("notes")
        editor.values["path"].set(str(path))
        editor.discover()
        wait()
        assert set(editor.catalog) == {"notes"}
        editor.table_tree.selection_set("notes")
        root.update()
        assert editor.states["notes"]["columns"] == set()
        editor._toggle_column("id")
        editor._toggle_column("body", index=True)
        editor.table_alias.set("备忘资料")
        editor.save()
        wait()
        assert editor.closed and len(saved) == 1
        assert saved[0]["allowed_columns"] == {"notes": ["body", "id"]}
        assert saved[0]["index"] == [{"table": "notes", "id_column": "id", "text_columns": ["body"]}]
        assert saved[0]["business_metadata"]["tables"]["notes"]["alias"] == "备忘资料"
    finally:
        if not editor.closed:
            editor.close()
        root.update()


@pytest.mark.skipif(os.name != "nt", reason="Real Tk dialog smoke runs on the validated Windows host")
def test_real_tk_changed_connection_cannot_save_old_discovery(tmp_path, database_ui_root):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
    source = {"id": "notes", "kind": "sqlite", "path": str(path), "allowed_tables": ["notes"], "allowed_columns": {"notes": ["id"]}}
    root = database_ui_root
    saved = []
    editor = open_database_editor(root, source, saved.append)
    editor.window.withdraw()
    try:
        from data_search.source_setup import _discover_source
        editor._loaded((connection_candidate(source, {"id": "notes", "kind": "sqlite", "path": str(path)}), _discover_source(source)))
        root.update()
        editor.values["path"].set(str(path) + ".different")
        editor.save()
        assert not saved and "重新发现" in editor.activity.get()
    finally:
        editor.close()
        root.update()
