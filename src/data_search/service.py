"""Single-user loopback daemon and authenticated local RPC client."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


class ServiceError(RuntimeError):
    pass


class InstanceLock:
    """A held OS lock, not PID existence, determines whether an instance exists."""
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ServiceError("A daemon already owns this data directory") from None
        self.handle = handle
        return self

    def __exit__(self, *_):
        if self.handle:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def _private_file(path: Path) -> bool:
    try:
        os.chmod(path, 0o600)
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
            identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                capture_output=True, text=True, check=True, timeout=5, creationflags=flags)
            sid = next(csv.reader(identity.stdout.strip().splitlines()))[1]
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
                capture_output=True, check=True, timeout=5, creationflags=flags)
        return True
    except (OSError, subprocess.SubprocessError, IndexError, StopIteration):
        return False


def _write_state(path: Path, state: dict):
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            # Harden before writing the token, especially for inherited Windows ACLs.
            state["acl_hardened"] = _private_file(temp)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _state_path(config):
    return Path(config["data_dir"]) / "service.json"


def _read_state(config):
    try:
        state = json.loads(_state_path(config).read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("port"), int) or not 1024 <= state["port"] <= 65535:
            raise ValueError()
        if not isinstance(state.get("pid"), int) or state["pid"] <= 0:
            raise ValueError()
        if not isinstance(state.get("token"), str) or len(state["token"]) < 32:
            raise ValueError()
        if not isinstance(state.get("service_id"), str) or not state["service_id"]:
            raise ValueError()
        configured_path = config.get("config_path")
        if configured_path and state.get("config_path") != str(Path(configured_path).resolve()):
            raise ServiceError("This data directory belongs to a daemon using another configuration")
        return state
    except (OSError, ValueError, KeyError):
        raise ServiceError("Service is not running, or its state file is invalid") from None


def _call_state(state, method, params, timeout=90):
    request = urllib.request.Request(f"http://127.0.0.1:{state['port']}/rpc",
        data=json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + state["token"]},
        method="POST")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ServiceError("Service response exceeds the client size limit")
        payload = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError):
        raise ServiceError("Service is unavailable or rejected the local request") from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise ServiceError(payload.get("error", "Service request failed") if isinstance(payload, dict) else "Invalid service response")
    return payload.get("result")


def rpc(config: dict, method: str, params: dict | None = None, *, timeout=90):
    return _call_state(_read_state(config), method, params or {}, timeout=timeout)


def service_status(config: dict):
    state = _read_state(config)
    health = _call_state(state, "_health", {}, timeout=2)
    if not isinstance(health, dict) or health.get("service_id") != state["service_id"] or health.get("pid") != state["pid"]:
        raise ServiceError("Service identity does not match its state file")
    return {**health, "port": state["port"], "acl_hardened": state.get("acl_hardened", False)}


class _RPCServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16


def run_daemon(config: dict, *, engine_factory=None):
    directory = Path(config["data_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    with InstanceLock(directory / "service.lock"):
        if engine_factory is None:
            from .engine import Engine
            engine_factory = Engine
        engine = engine_factory(config)
        token, service_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        server = None
        state = None
        request_slots = threading.BoundedSemaphore(4)
        closing = threading.Event()
        shutdown_lock = threading.Lock()
        requests_condition = threading.Condition()
        active_requests = 0
        allowed_methods = {"search", "fetch", "inspect_source", "query_database", "index_status", "scan", "pause", "resume"}

        def begin_shutdown():
            with shutdown_lock:
                if closing.is_set():
                    return
                # Dispatch admission and this transition share one lock, so a late
                # request cannot enter Engine after shutdown has begun.
                with requests_condition:
                    closing.set()
                cancel = getattr(engine, "begin_shutdown", None)
                if cancel is not None:
                    cancel()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(10)
                self._body_consumed = False

            def log_message(self, *_):
                pass

            def _discard_request_body(self):
                """Avoid a TCP reset discarding the rejection on Windows.

                Closing with a pending request body can reset the connection before
                the client receives our response. Drain only a bounded, explicitly
                sized body; neither malformed framing nor a slow sender may hold a
                rejection open indefinitely.
                """
                if self._body_consumed:
                    return
                self._body_consumed = True
                try:
                    lengths = self.headers.get_all("Content-Length", [])
                    if len(lengths) != 1:
                        return
                    remaining = int(lengths[0])
                    if not 0 <= remaining <= 1024 * 1024:
                        return
                    deadline = time.monotonic() + 0.5
                    while remaining:
                        timeout = deadline - time.monotonic()
                        if timeout <= 0:
                            break
                        self.connection.settimeout(timeout)
                        block = self.rfile.read1(min(remaining, 65536))
                        if not block:
                            break
                        remaining -= len(block)
                except (ValueError, OSError):
                    pass
                finally:
                    self.connection.settimeout(10)

            def _send(self, status, payload):
                self._discard_request_body()
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def do_GET(self):
                self._send(405, {"ok": False, "error": "POST required"})

            def do_OPTIONS(self):
                self._send(403, {"ok": False, "error": "Browser cross-origin access is disabled"})

            def do_POST(self):
                nonlocal active_requests
                expected_host = f"127.0.0.1:{self.server.server_port}"
                if self.path != "/rpc" or self.headers.get("Host") != expected_host:
                    self._send(403, {"ok": False, "error": "Invalid local RPC endpoint"})
                    return
                if self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") not in (None, "none"):
                    self._send(403, {"ok": False, "error": "Browser-origin requests are disabled"})
                    return
                authorization = self.headers.get("Authorization", "")
                if not hmac.compare_digest(authorization, "Bearer " + token):
                    self._send(401, {"ok": False, "error": "Authentication required"})
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json" or self.headers.get("Transfer-Encoding"):
                    self._send(400, {"ok": False, "error": "Expected a bounded JSON body"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                    if not 0 <= length <= 1024 * 1024:
                        raise ValueError()
                    self._body_consumed = True
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or set(payload) - {"method", "params"}:
                        raise ValueError()
                    method, params = payload.get("method"), payload.get("params", {})
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise ValueError()
                except (ValueError, OSError, UnicodeError):
                    self._send(400, {"ok": False, "error": "Malformed RPC request"})
                    return
                if method == "_health":
                    result = {"status": "stopping" if closing.is_set() else "running", "pid": os.getpid(), "service_id": service_id, "node_id": config["node_id"]}
                elif method == "_stop":
                    begin_shutdown()
                    self._send(200, {"ok": True, "result": {"status": "stopping"}})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                elif method not in allowed_methods:
                    self._send(400, {"ok": False, "error": "Unknown RPC method"})
                    return
                else:
                    node_id = params.get("node_id")
                    if node_id is not None and node_id != config["node_id"]:
                        self._send(200, {"ok": False, "error": "Remote nodes are reserved and not implemented"})
                        return
                    with requests_condition:
                        if closing.is_set():
                            self._send(200, {"ok": False, "error": "Service is shutting down"})
                            return
                        if not request_slots.acquire(blocking=False):
                            self._send(200, {"ok": False, "error": "Service is busy; retry later"})
                            return
                        active_requests += 1
                    try:
                        result = engine.dispatch(method, params)
                    except (ValueError, ServiceError) as error:
                        self._send(200, {"ok": False, "error": str(error)})
                        return
                    except Exception:
                        self._send(200, {"ok": False, "error": "Operation failed; check source availability and service status"})
                        return
                    finally:
                        request_slots.release()
                        with requests_condition:
                            active_requests -= 1
                            requests_condition.notify_all()
                self._send(200, {"ok": True, "result": result})

        try:
            server = _RPCServer(("127.0.0.1", 0), Handler)
            engine.start_background()
            state = {"pid": os.getpid(), "port": server.server_port, "token": token, "service_id": service_id,
                     "node_id": config["node_id"], "config_path": str(Path(config["config_path"]).resolve()),
                     "started_at": datetime.now(timezone.utc).isoformat()}
            _write_state(_state_path(config), state)
            server.serve_forever(poll_interval=0.1)
        finally:
            begin_shutdown()
            if server:
                server.server_close()
            try:
                with requests_condition:
                    while active_requests:
                        requests_condition.wait(timeout=0.2)
                engine.close()
            finally:
                if state:
                    try:
                        current = json.loads(_state_path(config).read_text(encoding="utf-8"))
                        if current.get("service_id") == service_id:
                            _state_path(config).unlink(missing_ok=True)
                    except (OSError, ValueError):
                        pass


def start_service(config: dict, *, timeout=30):
    try:
        return {**service_status(config), "started": False}
    except ServiceError:
        pass
    config_path = config.get("config_path")
    if not config_path:
        raise ServiceError("A saved configuration path is required to start the daemon")
    directory = Path(config["data_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "daemon.log"
    # Bound log growth between restarts; the RPC server does not log request contents.
    if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
        log_path.replace(directory / "daemon.previous.log")
    with log_path.open("ab") as log:
        args = [sys.executable, "-m", "data_search", "daemon", "--config", str(Path(config_path).resolve())]
        options = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(args, **options)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return {**service_status(config), "started": process.poll() is None}
        except ServiceError:
            time.sleep(0.1)
    raise ServiceError("Daemon did not become ready; check daemon.log in the configured data directory")


def stop_service(config: dict, *, timeout=15):
    try:
        health = service_status(config)
    except ServiceError:
        return {"status": "not_running", "stopped": False}
    state = _read_state(config)
    if health["service_id"] != state["service_id"]:
        raise ServiceError("Service changed while stopping; retry")
    _call_state(state, "_stop", {}, timeout=3)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = _read_state(config)
            if current["service_id"] != state["service_id"]:
                return {"status": "stopped", "stopped": True}
        except ServiceError:
            return {"status": "stopped", "stopped": True}
        time.sleep(0.1)
    raise ServiceError("Daemon is still shutting down; no unrelated process was terminated")
