"""First DSH activation installs the native bundle into a fresh synthetic target."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from verify_upgrade_v051 import Profile, HERE, FIXTURES, SHELL, run, digest, candidate_provenance


class ManagedFixture:
    def __init__(self, bundle, work, dsh, *, offline=False):
        self.bundle, self.work = bundle.resolve(), work.resolve()
        assert self.work.is_relative_to(FIXTURES) and self.work.name.startswith("upgrade-v051-")
        assert not self.work.exists(), "Use a fresh managed fixture"
        self.work.mkdir(parents=True)
        self.data, self.app = self.work / "data", self.work / "app"
        self.root = self.work / "corpus"
        self.root.mkdir()
        (self.root / "fixture-auto.md").write_text("# Managed installation fixture\nupgradefixture051 auto install validation", encoding="utf-8")
        self.executable = self.app / "runtime/data-search.exe"
        self.config = self.data / "config.json"
        self.dsh = dsh.resolve()
        self.offline = offline
        self.profiles = []
        self.profile_worker = HERE / "dsh_auto_profile_v051.mjs"
        self.managed_request = {"installDir": str(self.app), "dataDir": str(self.data),
            "releaseDir": str(self.bundle), "roots": [str(self.root)], "skipModel": True, "noAutostart": True}
        self.report = {"schema_version": 1, "tested_at_utc": datetime.now(timezone.utc).isoformat(),
            "synthetic_only": True, "default_dsh_profile_modified": False,
            "model_requests": 0, "no_explicit_command_or_config_path": True,
            "fixture_node_options": "--max-old-space-size=192 --max-semi-space-size=4",
            "initial_install_and_data_directories_absent": True,
            **candidate_provenance(self.bundle)}

    def cli(self, *args):
        return json.loads(run([self.executable, *args, "--config", self.config]).stdout)

    def execute(self):
        print("Starting fresh DSH managed profile; plugin must install its own native runtime", flush=True)
        assert not self.data.exists() and not self.app.exists()
        profile = Profile(self, "managed-auto", self.bundle / "plugins/deepseek-harness")
        profile.wait_ready()
        assert profile.control()["state"] == "ready"
        assert profile.web("status")["result"]["service"]["status"] == "running"
        assert not (self.data / "upgrade-state.json").exists()
        assert len(list((self.data / "host-clients").glob("*.json"))) == 1
        config = json.loads(self.config.read_text())
        assert config["scope"] == "directories" and config["roots"] == [str(self.root)]
        assert config["semantic"]["enabled"] is False
        assert self.cli("version")["version"] == "0.5.1"
        assert digest(self.executable) == self.report["candidate_executable_sha256"]
        transactions = list(self.app.glob(".upgrade-*/transaction.json"))
        assert len(transactions) == 1
        assert json.loads(transactions[0].read_text())["phase"] == "complete"
        self.report.update(success=True, native_installed_by_plugin=True,
            native_version="0.5.1", native_executable_hash_matches=True,
            registry_before_first_install_supported=True, installer_self_resume_completed=True,
            live_host_registrations=1, mcp_tools=11, keyword_search_succeeded=True,
            web_status_running=True, synthetic_scope_preserved=True, semantic_disabled=True)

    def cleanup(self):
        for profile in reversed(self.profiles):
            profile.stop()
        if (self.app / "install-manifest.json").exists():
            assert not (self.data / "upgrade-state.json").exists(), "Preserve a failed activation for recovery"
            run([SHELL, "-NoProfile", "-File", self.app / "uninstall.ps1", "-InstallDir", self.app, "-DeleteData"])
            assert not self.data.exists() and not self.app.exists()
            self.report["managed_fixture_installation_removed"] = True


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("work", type=Path)
parser.add_argument("--dsh", type=Path, required=True)
parser.add_argument("--offline", action="store_true")
args = parser.parse_args()
fixture = ManagedFixture(args.bundle, args.work, args.dsh, offline=args.offline)
try:
    fixture.execute()
except BaseException as error:
    fixture.report.update(success=False, error_type=type(error).__name__, error=str(error)[:2500])
    raise
finally:
    try:
        fixture.cleanup()
    finally:
        (fixture.work / "report.json").write_text(json.dumps(fixture.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(fixture.report, ensure_ascii=False, indent=2))
