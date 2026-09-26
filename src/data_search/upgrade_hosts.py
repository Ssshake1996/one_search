"""Local DSH maintenance barrier, owned by the separately extracted updater.

Hosts register before checking the durable marker. Consequently a host either
appears in the drain set or sees the marker before launching any native code.
Only the updater holding upgrade.lock may remove the marker and resume hosts.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import time
import uuid

import psutil

from .config import atomic_json
from .service import InstanceLock, ServiceError


class UpgradeCoordinationError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _read(path):
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Invalid upgrade coordination file")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Invalid upgrade coordination record")
    return value


def _control(host, action, transaction_id, timeout=60):
    # Do not use proxy-aware HTTP clients or an address supplied by a registry.
    connection = HTTPConnection("127.0.0.1", host["port"], timeout=timeout)
    try:
        body = json.dumps({"action": action, "transaction_id": transaction_id})
        connection.request("POST", "/control", body, {"Authorization": "Bearer " + host["token"],
            "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError("Host rejected maintenance")
        value = json.loads(raw)
        result = value.get("result", {})
        if value.get("ok") is not True or result.get("instance_id") != host["instance_id"]:
            raise ValueError("Host maintenance identity mismatch")
        if action == "prepare" and (not result.get("maintenance") or result.get("transaction_id") != transaction_id):
            raise ValueError("Host did not acknowledge maintenance")
        return result
    finally:
        connection.close()


class UpgradeSession:
    """Serialize installers and leave an interrupted activation closed to clients."""

    def __init__(self, install, data, *, recover=None, control=_control):
        self.install, self.data = Path(install).resolve(), Path(data).resolve()
        self.marker = self.data / "upgrade-state.json"
        self.lock = InstanceLock(self.data / "upgrade.lock")
        self.recover, self.control = recover, control
        self.hosts = []
        self.safe = True
        self.record = {"schema_version": 1, "transaction_id": uuid.uuid4().hex,
            "install_dir": str(self.install), "data_dir": str(self.data),
            "owner_pid": os.getpid(), "started_at": time.time(), "phase": "preparing"}

    def hosts_now(self):
        directory = self.data / "host-clients"
        if not directory.exists():
            return []
        if directory.is_symlink() or (os.name == "nt" and directory.lstat().st_file_attributes & 0x400):
            raise UpgradeCoordinationError("host_registry_invalid", "Host registry must not be a directory link")
        paths = list(directory.glob("*.json"))
        if len(paths) > 128:
            raise UpgradeCoordinationError("host_registry_invalid", "Too many host registrations; close unused DSH profiles")
        hosts = []
        for path in paths:
            try:
                host = _read(path)
                if (host.get("schema_version") != 1 or not isinstance(host.get("pid"), int) or
                    not isinstance(host.get("port"), int) or not 1024 <= host["port"] <= 65535 or
                    not isinstance(host.get("token"), str) or not 32 <= len(host["token"]) <= 256 or
                    not isinstance(host.get("instance_id"), str) or
                    Path(host.get("data_dir", "")).resolve() != self.data or
                    Path(host.get("config_path", "")).resolve() != self.data / "config.json"):
                    continue
                if psutil.pid_exists(host["pid"]):
                    hosts.append(host)
            except (OSError, ValueError, TypeError):
                continue
        return hosts

    def _drain(self):
        self.hosts = self.hosts_now()
        if not self.hosts:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(self.hosts))) as pool:
            futures = [pool.submit(self.control, host, "prepare", self.record["transaction_id"]) for host in self.hosts]
            errors = []
            for host, future in zip(self.hosts, futures):
                try:
                    future.result()
                except Exception as error:
                    errors.append((host, error))
        if errors:
            failure = UpgradeCoordinationError("host_prepare_failed",
                "A registered DSH controller could not release its MCP connection. Check the listed host PIDs and restart the corresponding profile; preserve the installation and retry")
            failure.details = [{"pid": host["pid"], "instance_id": host["instance_id"], "role": "dsh_host"} for host, _ in errors]
            raise failure from errors[0][1]

    def update(self, phase, *, transaction=None, safe=None):
        self.record["phase"] = phase
        if transaction is not None:
            self.record["transaction"] = str(transaction)
        if safe is not None:
            self.safe = safe
        atomic_json(self.marker, self.record)

    def __enter__(self):
        try:
            self.lock.__enter__()
        except ServiceError as error:
            raise UpgradeCoordinationError("upgrade_in_progress", "Another installer owns this instance; wait for it to finish") from error
        try:
            self.safe = not self.marker.exists()
            try:
                previous = _read(self.marker) if self.marker.exists() else None
            except (OSError, ValueError) as error:
                raise UpgradeCoordinationError("upgrade_recovery_required", "Maintenance marker is unreadable; preserve it and the retained transaction for recovery") from error
            if previous is not None and (previous.get("schema_version") != 1 or
                    not isinstance(previous.get("transaction_id"), str) or not 1 <= len(previous["transaction_id"]) <= 160 or
                    previous.get("phase") not in {"preparing", "staging", "snapshot_complete", "swapping", "activating",
                        "rolling_back", "restored", "rollback_blocked", "complete", "rolled_back", "failed_before_activation"}):
                raise UpgradeCoordinationError("upgrade_recovery_required", "Maintenance marker has an invalid schema; preserve it and the retained transaction for recovery")
            if previous and (previous.get("install_dir") != str(self.install) or previous.get("data_dir") != str(self.data)):
                raise UpgradeCoordinationError("upgrade_recovery_required", "Maintenance marker does not match this installation; preserve it for recovery")
            if previous:
                # Keep the interrupted transaction ID while newly started hosts drain.
                self.record["transaction_id"] = previous["transaction_id"]
                self.safe = False
                self.record.update({key: previous[key] for key in ("phase", "transaction") if key in previous})
            atomic_json(self.marker, self.record)
            self._drain()
            if previous:
                if self.recover is None:
                    raise UpgradeCoordinationError("upgrade_recovery_required", "An interrupted upgrade needs recovery before clients may reconnect")
                self.recover(previous, self)
                self.record.pop("transaction", None)
                self.update("preparing", safe=True)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        try:
            if self.safe:
                # Removing the durable marker is the commit point for admission.
                self.marker.unlink(missing_ok=True)
                try:
                    hosts = {host["instance_id"]: host for host in self.hosts + self.hosts_now()}
                except Exception:
                    hosts = {host["instance_id"]: host for host in self.hosts}
                for host in hosts.values():
                    try:
                        self.control(host, "resume", self.record["transaction_id"], timeout=3)
                    except Exception:
                        # A live host also polls the durable marker, so a lost finish
                        # request does not strand it. Never echo control credentials.
                        pass
        finally:
            self.lock.__exit__(*args)
