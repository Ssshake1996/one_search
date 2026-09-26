"""Real released-v0.5 -> candidate-v0.5.1 native/DSH acceptance.

Windows runner acceptance helper. It only installs inside a fresh, bounded synthetic
fixture, never modifies the default DSH home, and does not load an LLM/model.
Run only after the candidate runtime and plugin sources have been frozen.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading
import time
import zipfile

import psutil


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
FIXTURES = REPO / ".packaging-smoke"
PREFIX = "@@ONE_SEARCH_ACCEPTANCE@@"
QUERY = "upgradefixture051"
SHELL = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
RUNTIME_SOURCE_COMMIT = "efeff0ff123209b89d056fa41719737d08294992"
CANDIDATE_ARCHIVE_SHA256 = "240de589cbf5aa6947bd3733f08ba1efc907f6e557d91a2fd53345ef1fd4730f"
CANDIDATE_EXECUTABLE_SHA256 = "cacff5988147dd4e2081658462ab06fabb53d1677bd2e83f77380630ce366bd6"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def candidate_provenance(bundle):
    executable = digest(bundle / "runtime/data-search.exe")
    assert executable == CANDIDATE_EXECUTABLE_SHA256, "Use the pinned candidate binary, not a rebuilt executable"
    script_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    return {"candidate_executable_sha256": executable,
        "candidate_archive_sha256_expected_by_workflow": CANDIDATE_ARCHIVE_SHA256,
        "runtime_source_commit": RUNTIME_SOURCE_COMMIT, "acceptance_script_commit": script_commit,
        "provenance_note": "Runtime was built at runtime_source_commit; later helper/workflow commits do not rebuild it. The workflow verifies the archive before extraction."}


def run(command, *, check=True, input=None, timeout=180):
    result = subprocess.run([str(part) for part in command], input=input,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **HIDDEN)
    if check and result.returncode:
        raise AssertionError(result.stdout[-6000:] + result.stderr[-3000:])
    return result


def eventually(callback, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except (OSError, ValueError, AssertionError, KeyError) as error:
            last = error
        time.sleep(.25)
    raise AssertionError(f"Timed out waiting for acceptance condition: {last}")


def json_lines(text):
    values = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                values.append(value)
        except ValueError:
            pass
    return values


class Profile:
    def __init__(self, owner, name, bundle):
        self.owner, self.name = owner, name
        request = {"fixtureRoot": str(owner.work), "dshPackage": str(owner.dsh),
            "bundleDir": str(bundle), "home": str(owner.work / ("home-" + name)),
            "profile": "web", "instanceName": name,
            "command": str(owner.executable), "commandArgs": getattr(owner, "command_args", []),
            "configPath": str(owner.config), "offline": getattr(owner, "offline", False), "preRegistered": name in getattr(owner, "pre_registered", set())}
        if hasattr(owner, "managed_request"):
            for key in ["command", "commandArgs", "configPath"]:
                del request[key]
            request["managed"] = owner.managed_request
        path = owner.work / ("profile-" + name + ".json")
        path.write_text(json.dumps(request), encoding="utf-8")
        self.messages = queue.Queue()
        self.diagnostics = []
        worker = getattr(owner, "profile_worker", HERE / "dsh_upgrade_profile_v051.mjs")
        environment = os.environ.copy()
        environment["NODE_OPTIONS"] = "--max-old-space-size=192 --max-semi-space-size=4"
        self.process = subprocess.Popen(["node", str(worker), str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=environment, **HIDDEN)
        owner.profiles.append(self)
        self.closed = False
        self.started = False
        self.number = 0
        def drain():
            for line in self.process.stdout:
                # Host logs are deliberately not saved: only the bounded control
                # channel is needed and authentication material must stay private.
                if line.startswith(PREFIX):
                    item = json.loads(line[len(PREFIX):])
                    if item.get("event") == "diagnostic":
                        self.diagnostics.append(item)
                    else:
                        self.messages.put(item)
            self.messages.put({"event": "exited", "returncode": self.process.poll()})
        threading.Thread(target=drain, daemon=True).start()
        ready = self.messages.get(timeout=120)
        assert ready.get("event") == "ready", ready
        self.started = True
        self.ready = ready

    def call(self, action, **params):
        assert not self.closed
        self.number += 1
        identity = str(self.number)
        self.process.stdin.write(json.dumps({"id": identity, "action": action, **params}) + "\n")
        self.process.stdin.flush()
        response = self.messages.get(timeout=45)
        assert response.get("id") == identity and response.get("ok"), response
        return response["result"]

    def web(self, action, params=None):
        response = self.call("web_action", webAction=action, params=params or {})
        assert response["status"] == 200, response
        envelope = response["envelope"]
        assert envelope["result"]["ok"], envelope
        return envelope["result"]["value"]

    def search(self):
        result = self.call("mcp_search", query=QUERY)
        assert not result["isError"] and result["contains_query"], result
        # The keyword is fixture-only and must appear in an actual content block.
        contents = result["result"].get("content", [])
        assert any(QUERY in item.get("text", "") and "fixture-" in item.get("text", "")
                   for item in contents), result
        return True

    def wait_ready(self):
        try:
            eventually(lambda: len(self.call("state")["mcp_tools"]) == 11)
        except AssertionError as error:
            raise AssertionError(f"Profile {self.name}: {error}; runtime diagnostics: {self.diagnostics}") from error
        return eventually(self.search)

    def registration(self):
        matches = []
        for path in (self.owner.data / "host-clients").glob("*.json"):
            item = json.loads(path.read_text(encoding="utf-8-sig"))
            if item.get("pid") == self.process.pid:
                matches.append(item)
        assert len(matches) == 1, "Each live new host must register exactly once"
        result = matches[0]
        assert Path(result["config_path"]).resolve() == self.owner.config
        assert Path(result["data_dir"]).resolve() == self.owner.data
        assert result["schema_version"] == 1
        return result

    def control(self, action="status", transaction=None):
        record = self.registration()
        body = {"action": action}
        if transaction is not None:
            body["transaction_id"] = transaction
        connection = HTTPConnection("127.0.0.1", record["port"], timeout=60)
        try:
            connection.request("POST", "/control", json.dumps(body), {
                "Authorization": "Bearer " + record["token"], "Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read(65536))
            assert response.status == 200 and value.get("ok"), "Maintenance handshake failed"
            result = value["result"]
            assert result["instance_id"] == record["instance_id"]
            return result
        finally:
            connection.close()

    def maintenance(self):
        state = self.control()
        assert state["maintenance"] and state["state"] == "maintenance", state
        assert self.call("state")["mcp_tools"] == [], "MCP scope must release native process"
        value = self.web("status")
        assert value["ok"] and value["result"]["service"]["status"] == "maintenance", value
        rejected = self.web("scan")
        assert not rejected["ok"] and rejected["error"]["code"] == "upgrade_in_progress", rejected
        return True

    def stop(self):
        if self.closed:
            return
        if not self.started:
            self.process.stdin.close()
            self.process.wait(timeout=25)
            self.closed = True
            return
        if self.process.poll() is None:
            self.call("stop")
            self.process.wait(timeout=25)
        self.closed = True
        assert self.process.returncode == 0, f"Profile {self.name} did not exit cleanly"


class Acceptance:
    def __init__(self, args):
        self.bundle, self.work, self.dsh = args.bundle.resolve(), args.work.resolve(), args.dsh.resolve()
        self.offline = args.offline
        assert self.work.is_relative_to(FIXTURES) and self.work.name.startswith("upgrade-v051-")
        assert not self.work.exists(), "Use a fresh synthetic acceptance directory"
        assert json.loads((self.bundle / "RELEASE_MANIFEST.json").read_text())["version"] == "0.5.1"
        self.work.mkdir(parents=True)
        self.root = self.work / "升级 合成资料"
        self.root.mkdir()
        for index in range(2):
            (self.root / f"fixture-{index}.md").write_text(f"# Upgrade fixture {index}\n{QUERY} 服务器升级合成资料。", encoding="utf-8")
        self.app, self.data = self.work / "app", self.work / "data"
        self.executable, self.config = self.app / "runtime/data-search.exe", self.data / "config.json"
        self.profiles = []
        self.pre_registered = set()
        self.gui = None
        self.active_upgrade = None
        self.report = {"schema_version": 1, "tested_at_utc": datetime.now(timezone.utc).isoformat(),
            "from_version": "0.5.0", "to_version": "0.5.1", "synthetic_only": True,
            "default_dsh_profile_modified": False, "real_machine_scan_performed": False,
            "autostart": "disabled_for_test", "model_requests": 0,
            "fixture_node_options": "--max-old-space-size=192 --max-semi-space-size=4",
            "acceptance_resource_constraints": {
                "local_preparatory_attempts": "native-02 DSH registration exited 134; native-03/04 explicitly reported V8 commit/allocation failures; native-05 first shell launch failed before this helper ran",
                "local_preparatory_available_commit_gib": "0.7-1.1, despite about 15.8 GiB available physical memory",
                "mitigation": "Child-only Node heap bounds, profile staging before runtime startup, two persistent hosts and one short-lived entrant per transaction",
                "global_environment_or_unrelated_processes_modified": False},
            **candidate_provenance(self.bundle),
            "host_cli_version": json.loads((self.dsh / "package.json").read_text())["version"]}
        archive_path = REPO / "dist/release-v0.5.0/one-search-0.5.0-windows-amd64-native.zip"
        self.report["old_archive_sha256"] = digest(archive_path)
        assert self.report["old_archive_sha256"] == "b938f14af308a533d75e3c6da006ad55e870eca5d470d9a9d0e7a64883e024ae"
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.testzip() is None
            for member in archive.namelist():
                assert (self.work / "old-release" / member).resolve().is_relative_to(self.work / "old-release")
            archive.extractall(self.work / "old-release")
        self.old = self.work / "old-release" / archive_path.stem

    def installer(self, release):
        return [SHELL, "-NoProfile", "-File", release / "scripts/install.ps1", "-Root", self.root,
            "-InstallDir", self.app, "-DataDir", self.data, "-SkipModel", "-NoAutostart",
            "-Python", "system-python-must-not-be-used"]

    def stage_profiles(self):
        hashes = {}
        for name in ["old-v05", "new-a", "new-b", "rollback-entrant", "interrupted-entrant", "success-entrant"]:
            bundle = (self.old if name == "old-v05" else self.bundle) / "plugins/deepseek-harness"
            request = {"fixtureRoot": str(self.work), "dshPackage": str(self.dsh), "bundleDir": str(bundle),
                "home": str(self.work / ("home-" + name)), "offline": self.offline}
            path = self.work / ("stage-" + name + ".json")
            path.write_text(json.dumps(request), encoding="utf-8")
            result = run(["node", "--max-old-space-size=192", "--max-semi-space-size=4",
                HERE / "register_fixture_profile_v051.mjs", path])
            hashes[name] = json.loads(result.stdout)["bundle_sha256"]
            self.pre_registered.add(name)
        self.report["pre_registered_profile_bundles"] = hashes
        self.report["profiles_registered_before_any_runtime_started"] = True
        self.report["profile_registration_offline"] = self.offline

    def cli(self, *arguments):
        return json.loads(run([self.executable, *arguments, "--config", self.config]).stdout)

    def wait_index(self):
        return eventually(lambda: len(self.rows()) == 2 and
            self.cli("search", QUERY, "--mode", "keyword").get("results"))

    def rows(self):
        with closing(sqlite3.connect(f'{(self.data / "index.sqlite3").as_uri()}?mode=ro', uri=True)) as connection:
            return connection.execute("SELECT doc_id,text,locator FROM chunks ORDER BY id").fetchall()

    def service_id(self):
        return json.loads((self.data / "service.json").read_text())["service_id"]

    def assert_blocked(self, expected_role):
        before_id = self.service_id()
        before_config = self.config.read_bytes()
        before_exe = digest(self.executable)
        result = run(self.installer(self.bundle), check=False)
        failures = [item["error"] for item in json_lines(result.stdout) if isinstance(item.get("error"), dict)]
        matching = [failure for failure in failures if failure.get("code") == "runtime_in_use"]
        assert result.returncode and matching, result.stdout[-5000:] + result.stderr[-3000:]
        assert any(item["role"] == expected_role for failure in matching for item in failure["processes"]), matching
        assert self.service_id() == before_id, "Preflight rejection must not stop the original daemon"
        assert self.config.read_bytes() == before_config and digest(self.executable) == before_exe
        assert self.wait_index()
        assert not (self.data / "upgrade-state.json").exists()
        return {"error_code": "runtime_in_use", "blocking_role": expected_role,
            "same_service_id": True, "configuration_and_executable_unchanged": True, "search_available": True}

    def start_gui(self):
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        self.gui = subprocess.Popen([str(self.executable), "setup", "--config", str(self.config)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=info, **HIDDEN)
        def live_settings():
            if self.gui.poll() is not None:
                raise AssertionError("Synthetic settings window exited before runtime-use validation")
            result = run([self.bundle / "runtime/data-search.exe", "--internal-module", "data_search.runtime_use",
                "--install-dir", self.app, "--data-dir", self.data], check=False)
            value = json.loads(result.stdout)
            return any(item["role"] == "settings" for item in value.get("error", {}).get("processes", []))
        eventually(live_settings, timeout=20)

    def stop_gui(self):
        if self.gui is None:
            return
        try:
            parent = psutil.Process(self.gui.pid)
            processes = parent.children(recursive=True) + [parent]
            owned_pids = {process.pid for process in processes}
            console_host = (Path(os.environ["SystemRoot"]) / "System32/conhost.exe").resolve()
            targets = []
            for process in processes:
                try:
                    executable = Path(process.exe()).resolve()
                    if executable == console_host:
                        assert process.ppid() in owned_pids
                    else:
                        assert executable.is_relative_to(self.app / "runtime")
                        targets.append(process)
                except psutil.NoSuchProcess:
                    pass
            for process in targets:
                process.terminate()
            gone, alive = psutil.wait_procs(processes, timeout=10)
            assert not alive, "Synthetic GUI cleanup did not finish"
        except psutil.NoSuchProcess:
            pass
        self.gui = None

    def interrupt_owned_updater(self):
        """Terminate only the Popen updater tree held at our own continuation."""
        parent = psutil.Process(self.active_upgrade.pid)
        processes = [parent, *parent.children(recursive=True)]
        candidate = (self.bundle / "runtime/data-search.exe").resolve()
        shell = Path(SHELL).resolve()
        console_host = (Path(os.environ["SystemRoot"]) / "System32/conhost.exe").resolve()
        owned_pids = {process.pid for process in processes}
        checked = []
        for process in processes:
            executable = Path(process.exe()).resolve()
            arguments = process.cmdline()
            if executable == console_host:
                assert process.ppid() in owned_pids
                continue  # Windows disposes the exact captured console after its owner exits.
            assert executable in {candidate, shell}, "Unexpected child in the synthetic updater tree"
            assert any(str(self.work).lower() in part.lower() for part in arguments), "Updater child lacks fixture ownership"
            if executable == candidate:
                assert "data_search.upgrade" in arguments
            else:
                assert "-NativeTransactionChild" in arguments
            checked.append((process, executable, process.create_time()))
        assert any(executable == shell for _, executable, _ in checked)
        # Stop the updater first so killing its continuation cannot trigger a
        # concurrent rollback; then stop that exact captured continuation child.
        for process, executable, created in sorted(checked, key=lambda item: item[1] == shell):
            if process.is_running():
                assert process.create_time() == created
                process.terminate()
        gone, alive = psutil.wait_procs(processes, timeout=15)
        assert not alive, "An owned updater process survived the interruption"
        self.active_upgrade.communicate(timeout=10)
        self.active_upgrade = None
        return {"terminated_owned_processes": len(processes), "all_children_stopped": True,
            "selection": "captured exact Popen descendants, verified executable, fixture args and creation time"}

    def transaction(self, *, fail, profiles, interrupt=False):
        assert not (fail and interrupt)
        name = "interrupted" if interrupt else "rollback" if fail else "success"
        ready, release = self.work / (name + "-held"), self.work / (name + "-release")
        script = self.work / (name + "-continuation.ps1")
        script.write_text(r'''param([switch]$NativeTransactionChild,[string]$UpgradeRequest)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$request = Get-Content -LiteralPath $UpgradeRequest -Raw -Encoding UTF8 | ConvertFrom-Json
[IO.File]::WriteAllText($request.AcceptanceReadyPath, 'ready')
$deadline = [DateTime]::UtcNow.AddSeconds(100)
while (-not (Test-Path -LiteralPath $request.AcceptanceReleasePath)) {
    if ([DateTime]::UtcNow -gt $deadline) { throw 'Synthetic acceptance barrier timed out' }
    Start-Sleep -Milliseconds 100
}
if ($request.AcceptanceFail) {
    $configPath = Join-Path $request.DataDir 'config.json'
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $config.node_id = 'injected-failed-start'
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllBytes((Join-Path $request.DataDir 'index.sqlite3'), [Text.Encoding]::UTF8.GetBytes('incompatible synthetic index'))
    & (Join-Path $request.RuntimeDir 'data-search.exe') start --config $configPath
} else {
    & $request.AcceptanceInstaller -NativeTransactionChild -UpgradeRequest $UpgradeRequest
}
exit $LASTEXITCODE
''', encoding="utf-8")
        request = {"InstallDir": str(self.app), "DataDir": str(self.data), "RuntimeDir": str(self.bundle / "runtime"),
            "Installer": str(script), "PowerShell": SHELL, "Root": [str(self.root)], "Exclude": [],
            "Preset": "balanced", "ModelDir": "", "SkipModel": True, "NoAutostart": True,
            "AcceptanceReadyPath": str(ready), "AcceptanceReleasePath": str(release), "AcceptanceFail": fail,
            "AcceptanceInstaller": str(self.bundle / "scripts/install.ps1")}
        request_path = self.work / (name + "-request.json")
        request_path.write_text(json.dumps(request), encoding="utf-8")
        before_config, before_rows, before_hash = self.config.read_bytes(), self.rows(), digest(self.executable)
        before_transactions = set(self.app.glob(".upgrade-*"))
        self.active_upgrade = subprocess.Popen([str(self.bundle / "runtime/data-search.exe"),
            "--internal-module", "data_search.upgrade", "--request", str(request_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **HIDDEN)
        def barrier_ready():
            if self.active_upgrade.poll() is not None:
                out, err = self.active_upgrade.communicate()
                raise RuntimeError("Upgrade exited before the synthetic activation barrier: " + out[-5000:] + err[-2000:])
            return ready.exists()
        interrupted = False
        interruption = None
        try:
            eventually(barrier_ready)
            marker = json.loads((self.data / "upgrade-state.json").read_text())
            assert marker["transaction_id"] and marker["phase"] == "activating"
            for profile in profiles:
                profile.maintenance()
                assert profile.control("resume", marker["transaction_id"])["maintenance"]
            # Entrant arrives after native replacement, while activation is held.
            entrant = Profile(self, name + "-entrant", self.bundle / "plugins/deepseek-harness")
            eventually(entrant.maintenance)
            active_profiles = [*profiles, entrant]
            if interrupt:
                interruption = self.interrupt_owned_updater()
                interrupted = True
                assert (self.data / "upgrade-state.json").exists()
                assert not release.exists(), "Interrupted continuation must never have been released"
                for profile in active_profiles:
                    profile.maintenance()
        finally:
            if not interrupted:
                release.write_text("continue", encoding="utf-8")
        if interrupted:
            print("Retrying the normal incoming installer with the interrupted marker intact", flush=True)
            result = run(self.installer(self.bundle), check=False)
            out, err, code = result.stdout, result.stderr, result.returncode
        else:
            out, err = self.active_upgrade.communicate(timeout=180)
            code = self.active_upgrade.returncode
            self.active_upgrade = None
        assert (code != 0) == fail, out[-5000:] + err[-3000:]
        transactions = list(set(self.app.glob(".upgrade-*")) - before_transactions)
        assert len(transactions) == (2 if interrupted else 1)
        states = [json.loads((path / "transaction.json").read_text()) for path in transactions]
        if interrupted:
            assert sorted(state["phase"] for state in states) == ["complete", "rolled_back"], states
        state = next((state for state in states if state["phase"] == "complete"), states[0])
        assert state["phase"] == ("rolled_back" if fail else "complete"), state
        assert self.config.read_bytes() == before_config
        assert self.rows() == before_rows
        assert digest(self.executable) == (before_hash if fail else self.report["candidate_executable_sha256"])
        assert not (self.data / "upgrade-state.json").exists()
        for profile in active_profiles:
            profile.wait_ready()
            assert profile.control()["state"] == "ready"
            assert profile.web("status")["result"]["service"]["status"] == "running"
        assert self.cli("version")["version"] == ("0.5.0" if fail else "0.5.1")
        entrant.stop()
        return {"transaction_phase": state["phase"], "profiles_resumed": len(active_profiles),
            "maintenance_web_status_and_mutation_gate": True, "resume_blocked_while_marker_exists": True,
            "new_profile_during_activation_remained_in_maintenance": True,
            "configuration_and_index_rows_preserved": True, "all_mcp_searches_reconnected": True,
            "executable_hash_correct": True, "snapshot_retained": True,
            **({"interruption": interruption, "marker_preserved_until_normal_installer_recovered": True,
                "old_transaction_phase": "rolled_back", "new_transaction_phase": "complete"} if interrupted else {})}

    def execute(self, include_gui):
        print("Staging isolated profile packages before starting any runtime", flush=True)
        self.stage_profiles()
        print("Installing exact released v0.5 native into the isolated fixture", flush=True)
        run(self.installer(self.old))
        self.wait_index()
        old_profile = Profile(self, "old-v05", self.old / "plugins/deepseek-harness")
        old_profile.wait_ready()
        print("Checking old active DSH rejects upgrade without stopping its service", flush=True)
        self.report["old_host_blocked"] = self.assert_blocked("mcp")
        old_profile.search()
        old_service = self.service_id()
        old_profile.stop()
        assert self.service_id() == old_service
        profiles = [Profile(self, name, self.bundle / "plugins/deepseek-harness") for name in ["new-a", "new-b"]]
        for profile in profiles:
            profile.wait_ready()
            assert profile.control()["state"] == "ready"
        if include_gui:
            print("Checking a real native settings process remains a safe blocker", flush=True)
            try:
                self.start_gui()
                self.report["native_settings_blocked"] = self.assert_blocked("settings")
            finally:
                self.stop_gui()
            for profile in profiles:
                profile.wait_ready()
        print("Checking real failed activation, rollback and multi-profile reconnect", flush=True)
        self.report["failed_upgrade"] = self.transaction(fail=True, profiles=profiles)
        print("Checking interrupted updater recovery with the maintenance marker retained", flush=True)
        self.report["interrupted_upgrade_recovery"] = self.transaction(fail=False, profiles=profiles, interrupt=True)
        print("Checking successful upgrade with all active profiles plus maintenance entrant", flush=True)
        self.report["successful_upgrade"] = self.transaction(fail=False, profiles=profiles)
        service = self.service_id()
        profiles[0].stop()
        assert self.service_id() == service
        profiles[-1].search()
        self.report["one_profile_disposal_keeps_shared_daemon"] = True
        self.report["success"] = True

    def cleanup(self):
        self.stop_gui()
        # If a failed assertion interrupted the synthetic barrier, always release
        # our continuation so the real updater can complete or roll itself back.
        if self.active_upgrade is not None and self.active_upgrade.poll() is None:
            for name in ["success", "rollback", "interrupted"]:
                (self.work / (name + "-release")).write_text("continue", encoding="utf-8")
            self.active_upgrade.communicate(timeout=180)
        for profile in reversed(self.profiles):
            profile.stop()
        if (self.data / "upgrade-state.json").exists():
            # Preserve the actual interrupted journal/marker for diagnosis or a
            # normal installer retry. Never manufacture a successful cleanup.
            self.report["managed_fixture_installation_removed"] = False
            self.report["interrupted_fixture_retained_for_recovery"] = True
            return
        if (self.app / "install-manifest.json").exists():
            assert self.app.is_relative_to(self.work) and self.data.is_relative_to(self.work)
            run([SHELL, "-NoProfile", "-File", self.app / "uninstall.ps1", "-InstallDir", self.app, "-DeleteData"])
            assert not self.app.exists() and not self.data.exists()
        self.report["managed_fixture_installation_removed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--dsh", type=Path, required=True, help="Installed @deepseek-ai/dsh package directory")
    parser.add_argument("--offline", action="store_true", help="Require a prewarmed pnpm cache")
    parser.add_argument("--include-gui", action="store_true", help="Start a hidden, synthetic native settings process")
    args = parser.parse_args()
    owner = Acceptance(args)
    try:
        owner.execute(args.include_gui)
    except BaseException as error:
        owner.report["success"] = False
        owner.report["failure_type"] = type(error).__name__
        owner.report["failure"] = str(error)[:1500]
        raise
    finally:
        try:
            owner.cleanup()
        finally:
            (owner.work / "report.json").write_text(json.dumps(owner.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(owner.report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
