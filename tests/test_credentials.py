import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from data_search import credentials
from data_search.credentials import CredentialError
from data_search.databases import DatabaseError, DatabaseSource


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager requires a real Windows login")
def test_real_windows_vault_roundtrip_rotation_cross_process_and_delete():
    # Unique synthetic credential only; never reads or changes any existing entry.
    reference = credentials.store_credential("synthetic\n密码😀 ")
    try:
        assert reference.startswith("one-search-vault:windows:")
        assert credentials.read_credential(reference) == "synthetic\n密码😀 "
        assert credentials.credential_status(reference) == {"available": True, "backend": "windows", "code": "ready"}
        assert credentials.store_credential("rotated-synthetic", reference) == reference
        program = "from data_search.credentials import read_credential; import sys; assert read_credential(sys.argv[1]) == 'rotated-synthetic'; print('verified')"
        result = subprocess.run([sys.executable, "-c", program, reference], capture_output=True, text=True, timeout=5)
        assert result.returncode == 0 and result.stdout.strip() == "verified"
        assert credentials.resolve_password({"credential_ref": reference}) == "rotated-synthetic"
    finally:
        credentials.delete_credential(reference)
    assert not credentials.credential_status(reference)["available"]
    credentials.delete_credential(reference)


def test_linux_secret_service_uses_stdin_and_never_shell_or_plaintext(monkeypatch):
    monkeypatch.setattr(credentials, "_backend", lambda: "secret-service")
    monkeypatch.setattr(credentials.shutil, "which", lambda *_: "/usr/bin/secret-tool")
    calls, stored = [], {}
    def run(command, **options):
        calls.append((command, options))
        if command[1] == "store":
            stored[command[-1]] = options["input"]
        output = stored.get(command[-1], b"") if command[1] == "lookup" else b""
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"ignored")
    monkeypatch.setattr(credentials.subprocess, "run", run)
    secret = "do-not-log\n trailing space "
    reference = credentials.store_credential(secret)
    assert credentials.read_credential(reference) == secret
    credentials.store_credential("new-value", reference)
    assert credentials.read_credential(reference) == "new-value"
    credentials.delete_credential(reference)
    assert all(secret not in json.dumps(command) and not options.get("shell") for command, options in calls)
    assert calls[0][1]["input"] == secret.encode("utf-8")


def test_unavailable_or_locked_linux_vault_explicitly_fails_without_fallback(monkeypatch):
    monkeypatch.setattr(credentials, "_backend", lambda: "secret-service")
    monkeypatch.setattr(credentials.shutil, "which", lambda *_: None)
    with pytest.raises(CredentialError) as error:
        credentials.store_credential("synthetic")
    assert error.value.code == "vault_unavailable"
    monkeypatch.setattr(credentials.shutil, "which", lambda *_: "/usr/bin/secret-tool")
    monkeypatch.setattr(credentials.subprocess, "run", lambda *_args, **_kw: SimpleNamespace(returncode=1, stdout=b"", stderr=b"SECRET"))
    with pytest.raises(CredentialError) as error:
        credentials.store_credential("synthetic")
    assert error.value.code == "credential_missing_or_locked" and "SECRET" not in str(error.value)


def test_environment_remains_explicit_and_conflicting_sources_fail(monkeypatch):
    monkeypatch.setenv("ONE_SEARCH_SYNTHETIC_DB_PASSWORD", "synthetic")
    assert credentials.resolve_password({"password_env": "ONE_SEARCH_SYNTHETIC_DB_PASSWORD"}) == "synthetic"
    with pytest.raises(CredentialError):
        credentials.resolve_password({"password_env": "ONE_SEARCH_SYNTHETIC_DB_PASSWORD", "credential_ref": "invalid"})
    with pytest.raises(DatabaseError, match="not both"):
        DatabaseSource({"id": "db", "kind": "mysql", "password_env": "X", "credential_ref": "one-search-vault:windows:" + "0" * 32})
    with pytest.raises(DatabaseError, match="plaintext"):
        DatabaseSource({"id": "db", "kind": "mysql", "password": "never-accepted"})


@pytest.mark.parametrize("reference", ["", "one-search-vault:windows:../../user", "other-product", None])
def test_only_our_generated_reference_namespace_can_be_read_or_deleted(reference):
    with pytest.raises(CredentialError):
        credentials.read_credential(reference)
    with pytest.raises(CredentialError):
        credentials.delete_credential(reference)
