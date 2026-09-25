import asyncio
import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from data_search import cli
from data_search.config import defaults
from data_search.service import (
    InstanceLock, ServiceError, _call_state, _read_state,
    rpc, run_daemon, service_status, start_service, stop_service,
)


class FakeEngine:
    def __init__(self, config):
        self.calls = []
        self.started = self.closed = False

    def start_background(self):
        self.started = True

    def close(self):
        self.closed = True

    def dispatch(self, method, params):
        if params.get("query") == "invalid":
            raise ValueError("Invalid query")
        if params.get("query") == "private-error":
            raise RuntimeError("SECRET DRIVER ERROR")
        self.calls.append((method, params))
        return {"method": method, "params": params, "items": []}


@pytest.fixture
def daemon(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    config = defaults(str(tmp_path / "data"), [str(root)])
    config["config_path"] = str(tmp_path / "config.json")
    Path(config["config_path"]).write_text(json.dumps(config), encoding="utf-8")
    engine, errors = FakeEngine(config), []

    def run():
        try:
            run_daemon(config, engine_factory=lambda _: engine)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if errors:
            raise errors[0]
        try:
            state = _read_state(config)
            service_status(config)
            break
        except ServiceError:
            time.sleep(.05)
    else:
        pytest.fail("Fake daemon did not start")
    try:
        yield config, engine, state
    finally:
        if thread.is_alive():
            _call_state(state, "_stop", {})
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert engine.closed


def send(state, body=None, *, headers=None, path="/rpc", method="POST"):
    connection = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=3)
    try:
        request_headers = {"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request(method, path, body=body or json.dumps({"method": "index_status", "params": {}}), headers=request_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_daemon_lifecycle_and_dispatch(daemon):
    config, engine, state = daemon
    assert engine.started
    assert service_status(config)["service_id"] == state["service_id"]
    result = rpc(config, "search", {"query": "材料", "mode": "keyword"})
    assert result["params"]["query"] == "材料"
    assert engine.calls[-1] == ("search", {"query": "材料", "mode": "keyword"})
    assert "token" not in service_status(config)
    assert start_service(config)["started"] is False
    assert stop_service(config)["stopped"] is True
    assert stop_service(config) == {"status": "not_running", "stopped": False}


@pytest.mark.parametrize("headers,status", [
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Host": "evil.example"}, 403),
    ({"Origin": "http://evil.example"}, 403),
    ({"Origin": "null"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Content-Type": "text/plain"}, 400),
    ({"Transfer-Encoding": "chunked"}, 400),
])
def test_rejects_unauthorized_or_browser_requests(daemon, headers, status):
    _, engine, state = daemon
    code, payload = send(state, headers=headers)
    assert code == status and payload["ok"] is False
    assert engine.calls == []


@pytest.mark.parametrize("header,value,expected_status", [
    ("Origin", "null", 403),
    ("Origin", "http://evil.example", 403),
    ("Authorization", "Bearer wrong", 401),
    ("Content-Type", "text/plain", 400),
])
def test_rejected_headers_and_body_in_separate_writes_receive_response(daemon, header, value, expected_status):
    """Pending body bytes must not turn a deliberate rejection into a Windows RST."""
    _, engine, state = daemon
    body = json.dumps({"method": "index_status", "params": {}}).encode()
    for _ in range(10):
        connection = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=3)
        try:
            connection.putrequest("POST", "/rpc")
            headers = {"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"}
            headers[header] = value
            for key, item in headers.items():
                connection.putheader(key, item)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders()
            # Force the server to see the headers before any body is available.
            time.sleep(.01)
            connection.send(body)
            response = connection.getresponse()
            assert response.status == expected_status
            assert json.loads(response.read())["ok"] is False
        finally:
            connection.close()
    assert not engine.calls


def test_rejected_slow_body_has_bounded_drain_time(daemon):
    _, _, state = daemon
    connection = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=3)
    try:
        connection.putrequest("POST", "/rpc")
        connection.putheader("Origin", "null")
        connection.putheader("Content-Length", "100")
        connection.endheaders()
        started = time.monotonic()
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["ok"] is False
        assert time.monotonic() - started < 2
    finally:
        connection.close()


@pytest.mark.parametrize("body", ["not json", "[]", '{"method":5}', '{"method":"search","params":[]}', '{"method":"search","sql":"DROP"}'])
def test_malformed_payload_rejected(daemon, body):
    _, _, state = daemon
    assert send(state, body)[0] == 400


def test_endpoint_and_method_restrictions(daemon):
    _, _, state = daemon
    assert send(state, path="/unrelated")[0] == 403
    assert send(state, method="GET")[0] == 405
    assert send(state, method="OPTIONS")[0] == 403
    assert send(state, json.dumps({"method": "execute_shell"}))[0] == 400


def test_remote_node_and_errors(daemon):
    config, _, _ = daemon
    with pytest.raises(ServiceError, match="Remote nodes"):
        rpc(config, "search", {"query": "a", "node_id": "remote"})
    with pytest.raises(ServiceError, match="Invalid query"):
        rpc(config, "search", {"query": "invalid"})
    with pytest.raises(ServiceError) as error:
        rpc(config, "search", {"query": "private-error"})
    assert "SECRET" not in str(error.value)


def test_duplicate_daemon_rejected_by_os_lock(daemon):
    config, _, _ = daemon
    with pytest.raises(ServiceError, match="already owns"):
        run_daemon(config, engine_factory=FakeEngine)


def test_shutdown_rejects_new_requests_and_drains_existing_dispatch(daemon):
    config, engine, _ = daemon
    entered, shutdown_entered = threading.Event(), threading.Event()
    release_query, release_cancel, finished = threading.Event(), threading.Event(), threading.Event()
    outcomes = []

    def blocking_dispatch(method, params):
        entered.set()
        assert release_query.wait(5)
        finished.set()
        return {"completed": True}

    def begin_shutdown():
        shutdown_entered.set()
        assert release_cancel.wait(5)

    def checked_close():
        assert finished.is_set(), "Engine/store closed while a dispatch was still using it"
        engine.closed = True

    engine.dispatch = blocking_dispatch
    engine.begin_shutdown = begin_shutdown
    engine.close = checked_close
    request_thread = threading.Thread(target=lambda: outcomes.append(rpc(config, "search", {"query": "pending"})))
    request_thread.start()
    assert entered.wait(3)
    stop_thread = threading.Thread(target=lambda: outcomes.append(stop_service(config)))
    stop_thread.start()
    try:
        assert shutdown_entered.wait(3)
        assert not engine.closed
        with pytest.raises(ServiceError, match="shutting down"):
            rpc(config, "index_status")
    finally:
        release_query.set()
        release_cancel.set()
        request_thread.join(5)
        stop_thread.join(5)
    assert not request_thread.is_alive() and not stop_thread.is_alive()
    assert {"completed": True} in outcomes
    assert {"status": "stopped", "stopped": True} in outcomes


def test_lock_recovers_after_release(tmp_path):
    path = tmp_path / "instance.lock"
    with InstanceLock(path):
        with pytest.raises(ServiceError):
            with InstanceLock(path):
                pass
    with InstanceLock(path):
        assert path.exists()


def test_state_identity_checked_before_stop(daemon):
    config, _, original = daemon
    path = Path(config["data_dir"]) / "service.json"
    modified = {**original, "service_id": "unrelated-process"}
    try:
        path.write_text(json.dumps(modified), encoding="utf-8")
        with pytest.raises(ServiceError, match="identity"):
            service_status(config)
        assert stop_service(config)["stopped"] is False
    finally:
        path.write_text(json.dumps(original), encoding="utf-8")
    assert service_status(config)["status"] == "running"


def test_cli_init_never_overwrites_and_supports_repeated_roots(tmp_path, capsys):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(); two.mkdir()
    config = tmp_path / "config.json"
    args = ["init", "--config", str(config), "--data-dir", str(tmp_path / "index"), "--root", str(one), "--root", str(two)]
    assert cli.main(args) == 0
    original = config.read_bytes()
    assert json.loads(original)["roots"] == [str(one.resolve()), str(two.resolve())]
    assert cli.main(args) == 1
    assert config.read_bytes() == original
    assert "data-search:" in capsys.readouterr().err


def test_cli_json_stdout_and_config_position(daemon, capsys, tmp_path):
    config, _, _ = daemon
    assert cli.main(["--config", config["config_path"], "search", "材料", "--mode", "keyword"]) == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["params"]["mode"] == "keyword"
    request_path = tmp_path / "request.json"
    request_path.write_text('{"table":"orders"}', encoding="utf-8")
    assert cli.main(["query", "--source", "test", "--request", "@" + str(request_path), "--config", config["config_path"]]) == 0
    assert json.loads(capsys.readouterr().out)["params"]["request"] == {"table": "orders"}


def test_cli_status_requires_running_daemon(tmp_path, capsys):
    config = defaults(str(tmp_path / "data"), [])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert cli.main(["status", "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['error']['code']=='ServiceError' and "not running" in output.err


def test_mcp_official_sdk_tools_and_proxy(daemon):
    from data_search.mcp_server import create_mcp
    config, _, _ = daemon
    mcp = create_mcp(config)
    async def check():
        tools = await mcp.list_tools()
        readonly = {"search", "fetch", "inspect_source", "query_database", "index_status",'diagnose_path','read_context'}
        assert {tool.name for tool in tools} == readonly|{'refresh_path','prioritize_path','pause_indexing','resume_indexing'}
        assert all(tool.annotations.readOnlyHint==(tool.name in readonly) for tool in tools)
        result = await mcp.call_tool("search", {"query": "材料", "mode": "keyword"})
        # Official SDK returns content + structured output for a dict-returning tool.
        structured = result[1] if isinstance(result, tuple) else result
        if isinstance(structured, list):
            structured = json.loads(structured[0].text)
        assert structured["params"]["query"] == "材料"
    asyncio.run(check())
