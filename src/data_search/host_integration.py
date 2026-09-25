"""Explicit-path registration for hosts using the standard mcpServers JSON shape.

DSH Cordis profiles use their own integration. This helper never guesses a host
configuration path, edits a global marketplace, or adds credentials.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import uuid


def register_mcp(config_path: str | Path, server_entry: dict, server_name="data-search", *, replace=False):
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        raise ValueError("An explicit absolute host configuration path is required")
    if not isinstance(server_name, str) or not server_name or len(server_name) > 64:
        raise ValueError("Invalid server name")
    if not isinstance(server_entry, dict) or not isinstance(server_entry.get("command"), str):
        raise ValueError("MCP entry requires an executable command")
    executable = Path(server_entry["command"])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("MCP command must be an existing absolute executable path")
    if not isinstance(server_entry.get("args", []), list) or any(not isinstance(arg, str) for arg in server_entry.get("args", [])):
        raise ValueError("MCP args must be strings")
    if "env" in server_entry:
        raise ValueError("Register the local service command without embedding credentials")
    for ancestor in [path, *path.parents]:
        if ancestor.exists() and (ancestor.is_symlink() or (os.name == "nt" and ancestor.lstat().st_file_attributes & 0x400)):
            raise ValueError("Host configuration path must not use links")
    if not path.parent.is_dir():
        raise ValueError("Host configuration parent directory must already exist")
    original = path.read_bytes() if path.exists() else None
    data = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError("Host configuration must use an mcpServers JSON object; this is not a DSH profile adapter")
    updated = deepcopy(data)
    servers = updated.setdefault("mcpServers", {})
    existing = servers.get(server_name)
    if existing == server_entry:
        return {"changed": False, "path": str(path), "server": server_name, "backup": None}
    if server_name in servers and not replace:
        raise ValueError("This host already has a different server entry; explicit replace is required")
    servers[server_name] = deepcopy(server_entry)
    backup = None
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError("Host configuration changed during preparation; retry after its editor has saved")
        if original is not None:
            backup = path.with_name(path.name + ".one-search-" + uuid.uuid4().hex + ".bak")
            backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(backup_fd, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"changed": True, "path": str(path), "server": server_name, "backup": str(backup) if backup else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-config", type=Path, required=True)
    parser.add_argument("--mcp-config", type=Path, required=True, help="Installer-generated mcp.json")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    source = json.loads(args.mcp_config.read_text(encoding="utf-8-sig"))
    result = register_mcp(args.host_config, source["mcpServers"]["data-search"], replace=args.replace)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
