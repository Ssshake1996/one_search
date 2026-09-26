"""Read-only inventory of processes that can hold an installed runtime open.

This is a point-in-time check, not an admission lock. A cooperating MCP host must
remain in maintenance until the transaction finishes; an unrelated client can
still start a process after this check. Never terminate processes here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psutil


class RuntimeInUseError(RuntimeError):
    code = "runtime_in_use"

    def __init__(self, processes):
        self.details = processes
        super().__init__("Installed runtime is in use. Disconnect its MCP clients and close its settings window before retrying.")


def _path(value):
    return Path(os.path.abspath(os.path.expanduser(str(value)))).resolve()


def _inside(value, roots, *, lexical=False):
    if not value:
        return False
    try:
        candidate = Path(os.path.abspath(str(value))) if lexical else _path(value)
        return any(candidate == root or candidate.is_relative_to(root) for root in roots)
    except (OSError, ValueError):
        return False


def _read(path):
    try:
        # State files are small; do not accidentally read an arbitrary large file.
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.loads(stream.read(128 * 1024))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _argument(args, name):
    values = []
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            values.append(args[index + 1])
        elif value.startswith(name + "="):
            values.append(value[len(name) + 1:])
    return values[0] if len(values) == 1 else None


def _role(args):
    if args is None:
        return "unknown"
    module = "data_search"
    rest = args[1:]
    if rest[:1] in (["-m"], ["--internal-module"]):
        if len(rest) < 2:
            return "unknown"
        module, rest = rest[1], rest[2:]
    elif rest[:1] and rest[0].endswith((".py", ".pyw")):
        return "other"
    if module == "data_search.setup_ui":
        return "settings"
    if module == "data_search.worker":
        return "worker"
    if module == "data_search.preflight":
        return "preflight"
    if module == "data_search.model_manager":
        return "model_worker" if rest[:1] == ["worker"] else "cli"
    if module not in ("data_search", "data_search.cli"):
        return "other"
    # --config is accepted before or after the subcommand. Only the first
    # positional token is a command; query text or paths must not become roles.
    command = None
    index = 0
    while index < len(rest):
        if rest[index] == "--config":
            index += 2
        elif rest[index].startswith("--config="):
            index += 1
        else:
            command = rest[index]
            break
    return {"daemon": "daemon", "mcp": "mcp", "setup": "settings"}.get(command,
        "settings" if not rest else "cli")


def _same_path(value, path):
    try:
        return bool(value) and _path(value) == path
    except (OSError, ValueError):
        return False


def _process_field(process, field):
    try:
        return getattr(process, field)()
    except psutil.AccessDenied:
        return None


def _managed_roots(records, data):
    config_path = data / "config.json"
    config = _read(config_path)
    if not _same_path(config.get("data_dir"), data):
        return set()
    daemon = _read(data / "service.json")
    model = _read(data / "model-job/status.json")
    request = _read(data / "model-job/request.json")
    trusted = set()
    semantic = config.get("semantic")
    model_dir = semantic.get("model_dir") if isinstance(semantic, dict) else None
    for pid, item in records.items():
        args = item["args"]
        if args is None or not _same_path(_argument(args, "--config"), config_path):
            continue
        if item["role"] == "daemon" and daemon.get("pid") == pid and _same_path(daemon.get("config_path"), config_path):
            trusted.add(pid)
        if (item["role"] == "model_worker" and model.get("pid") == pid
                and model.get("state") in {"queued", "running", "cancelling"}
                and model.get("job_id") and model["job_id"] == request.get("job_id") == _argument(args, "--job")
                and model_dir and _same_path(model.get("model_dir"), _path(model_dir))):
            trusted.add(pid)
    # PyInstaller's outer bootloader can be the parent of the state-file PID.
    # Admit only identical executable/role/config parents, never a parent shell.
    for pid in list(trusted):
        current = records[pid]
        visited = {pid}
        while current["parent_pid"] in records and current["parent_pid"] not in visited:
            parent = records[current["parent_pid"]]
            if (parent["exe"] != current["exe"] or parent["role"] != current["role"]
                    or parent["args"] is None or not _same_path(_argument(parent["args"], "--config"), config_path)
                    or (parent["role"] == "model_worker" and _argument(parent["args"], "--job") != _argument(current["args"], "--job"))):
                break
            trusted.add(parent["pid"])
            visited.add(parent["pid"])
            current = parent
    # Child extractors are stopped by their managed owner. Explicit MCP, GUI,
    # and arbitrary CLI roles stay blocking even if launched by that owner.
    changed = True
    while changed:
        changed = False
        for pid, item in records.items():
            if pid not in trusted and item["parent_pid"] in trusted and item["role"] in {"worker", "preflight"}:
                trusted.add(pid)
                changed = True
    return trusted


def inspect_runtime_users(install_dir, data_dir, *, allow_service_workers=True):
    """Return safe process metadata for runtime/venv users, including blockers.

    When executable identity is denied, a process bearing a relevant executable
    name remains a blocker. Other installations with a readable executable path
    are excluded. The caller must run from outside the installation being changed.
    """
    install, data = _path(install_dir), _path(data_dir)
    roots = [install / "runtime", install / "venv"]
    relevant_names = {"data-search.exe", "data-search"}
    if roots[1].exists():
        relevant_names.update({"python.exe", "pythonw.exe", "python", "python3"})
    records = {}
    for process in psutil.process_iter():
        try:
            name = _process_field(process, "name")
            parent_pid = _process_field(process, "ppid")
            exe = _process_field(process, "exe")
            args = _process_field(process, "cmdline")
            executable_owned = _inside(exe, roots)
            launcher_owned = bool(args) and _inside(args[0], roots, lexical=True)
            uncertain = not exe and (name or "").lower() in relevant_names
            if not (executable_owned or launcher_owned or uncertain):
                continue
            relative = None
            if executable_owned or launcher_owned:
                candidate = _path(exe) if executable_owned else Path(os.path.abspath(args[0]))
                relative = candidate.relative_to(install).as_posix()
            records[process.pid] = {"pid": process.pid, "parent_pid": parent_pid,
                "name": name or (Path(exe).name if exe else "unknown"), "role": _role(args), "executable": relative,
                "exe": exe, "args": args, "uncertain": uncertain or args is None or name is None or parent_pid is None}
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    trusted = _managed_roots(records, data) if allow_service_workers else set()
    result = []
    for pid, item in sorted(records.items()):
        managed = pid in trusted and not item["uncertain"]
        result.append({key: item[key] for key in ("pid", "parent_pid", "name", "role", "executable")} | {
            "blocking": not managed,
            "reason": "managed_service" if managed else "identity_unavailable" if item["uncertain"] else "active_runtime_user"})
    return result


def assert_runtime_available(install_dir, data_dir, *, allow_service_workers=True):
    processes = inspect_runtime_users(install_dir, data_dir, allow_service_workers=allow_service_workers)
    blockers = [item for item in processes if item["blocking"]]
    if blockers:
        raise RuntimeInUseError(blockers)
    return processes


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check installation runtime users without stopping processes")
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--strict", action="store_true", help="Require managed service workers to have exited too")
    args = parser.parse_args(argv)
    try:
        result = assert_runtime_available(args.install_dir, args.data_dir, allow_service_workers=not args.strict)
        print(json.dumps({"ok": True, "processes": result}, ensure_ascii=True))
        return 0
    except RuntimeInUseError as error:
        print(json.dumps({"ok": False, "error": {"code": error.code, "message": str(error), "processes": error.details}}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
