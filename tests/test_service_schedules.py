from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest

from data_search.service_schedules import ScheduleError, ScheduleManager, SystemScheduler


class FakeScheduler:
    kind = "test"

    def __init__(self):
        self.tasks = {}
        self.fail_once = False
        self.fail_always = False

    def put(self, task):
        self.tasks[task["id"]] = dict(task)
        if self.fail_once or self.fail_always:
            self.fail_once = False
            raise ScheduleError("scheduler_rejected", "denied")

    def remove(self, task_id):
        if self.fail_always:
            raise ScheduleError("scheduler_rejected", "denied")
        self.tasks.pop(task_id, None)

    def cleanup(self, *_):
        pass


@pytest.fixture
def setup(tmp_path):
    now = [datetime(2030, 6, 3, 8, 0, tzinfo=timezone.utc)]
    config = {"data_dir": str(tmp_path), "config_path": str(tmp_path / "config.json")}
    adapter = FakeScheduler()
    manager = ScheduleManager(config, adapter=adapter, clock=lambda: now[0])
    return manager, adapter, now


def request(now, **overrides):
    return {"name": "工作日资料", "enabled": True,
            "schedule": {"kind": "once", "at": (now + timedelta(minutes=2)).isoformat()}, **overrides}


def saved(manager, adapter, now):
    value = manager.save(request(now))
    task_id = value["tasks"][0]["id"]
    return task_id, adapter.tasks[task_id]["generation"]


def test_defaults_empty_and_timezone_explicit(setup):
    manager, adapter, now = setup
    result = manager.list()
    assert result["revision"] == 0 and result["tasks"] == [] and adapter.tasks == {}
    assert len(result["timezone"]["offset"]) == 6
    assert result["timezone"]["now"].endswith(result["timezone"]["offset"])
    assert not manager.path.exists()


@pytest.mark.parametrize("changes", [
    {"enabled": 1}, {"name": "\nwrong"}, {"name": " "}, {"unknown": 1},
    {"schedule": {"kind": "once", "at": "bad timestamp"}},
    {"schedule": {"kind": "once", "at": "2020-01-01T12:00:00Z"}},
    {"schedule": {"kind": "daily", "time": "24:00"}},
    {"schedule": {"kind": "weekly", "time": "08:00", "weekdays": [True]}},
    {"schedule": {"kind": "weekly", "time": "08:00", "weekdays": [1, 1]}},
    {"schedule": {"kind": "weekly", "time": "08:00", "weekdays": []}},
    {"schedule": {"kind": "daily", "time": "08:00", "command": "shell"}},
])
def test_validation_happens_before_registering(setup, changes):
    manager, adapter, now = setup
    with pytest.raises(ScheduleError) as error:
        manager.save(request(now[0], **changes))
    assert error.value.code == "schedule_invalid"
    assert adapter.tasks == {} and not manager.path.exists()


def test_compare_and_swap_and_limit(setup):
    manager, adapter, now = setup
    manager.save(request(now[0]), revision=0)
    with pytest.raises(ScheduleError, match="已更新"):
        manager.save(request(now[0]), revision=0)
    for _ in range(31):
        manager.save(request(now[0]))
    with pytest.raises(ScheduleError) as error:
        manager.save(request(now[0]))
    assert error.value.code == "schedule_limit" and len(adapter.tasks) == 32


def test_due_is_checked_and_one_occurrence_cannot_start_twice(setup):
    manager, adapter, now = setup
    task_id, generation = saved(manager, adapter, now[0])
    calls = []
    start = lambda *args, **kwargs: calls.append(kwargs)
    assert manager.run_due(task_id, generation, starter=start)["code"] == "schedule_not_due"
    now[0] += timedelta(minutes=2)
    assert manager.run_due(task_id, generation, starter=start)["status"] == "started"
    assert manager.run_due(task_id, generation, starter=start)["code"] == "schedule_already_fired"
    assert calls == [{"reason": "scheduled"}]
    value = manager.list()
    assert value["revision"] == 1
    assert value["tasks"][0]["last_result"] == {"status": "started"}
    assert value["tasks"][0]["next_run_at"] is None


def test_edit_disable_delete_reject_queued_callbacks(setup):
    manager, adapter, now = setup
    task_id, generation = saved(manager, adapter, now[0])
    manager.save(request(now[0], id=task_id, enabled=False), revision=1)
    now[0] += timedelta(minutes=2)
    assert manager.run_due(task_id, generation)["code"] == "schedule_inactive"
    manager.delete(task_id, revision=2)
    assert manager.run_due(task_id, generation)["code"] == "schedule_inactive"
    assert not adapter.tasks and manager.list()["tasks"] == []


def test_partial_os_registration_failure_rolls_back_old_generation(setup):
    manager, adapter, now = setup
    task_id, generation = saved(manager, adapter, now[0])
    adapter.fail_once = True
    with pytest.raises(ScheduleError) as error:
        manager.save(request(now[0], id=task_id, name="modified"), revision=1)
    assert error.value.code == "scheduler_rejected"
    assert adapter.tasks[task_id]["generation"] == generation
    assert manager.list()["revision"] == 1 and not manager.journal.exists()


def test_failed_rollback_stays_closed_until_recovery(setup):
    manager, adapter, now = setup
    task_id, generation = saved(manager, adapter, now[0])
    adapter.fail_always = True
    with pytest.raises(ScheduleError) as error:
        manager.delete(task_id)
    assert error.value.code == "schedule_recovery_required"
    now[0] += timedelta(minutes=2)
    assert manager.run_due(task_id, generation)["code"] == "schedule_recovery_required"
    adapter.fail_always = False
    assert manager.list()["tasks"][0]["id"] == task_id
    assert not manager.journal.exists()


def test_new_registration_failure_leaves_no_record_or_task(setup):
    manager, adapter, now = setup
    adapter.fail_once = True
    with pytest.raises(ScheduleError):
        manager.save(request(now[0]))
    assert manager.list()["tasks"] == [] and adapter.tasks == {}


def test_restart_recovers_unfinished_edit(setup):
    manager, adapter, now = setup
    task_id, generation = saved(manager, adapter, now[0])
    previous = json.loads(manager.path.read_text(encoding="utf-8"))
    manager.journal.write_text(json.dumps({"id": task_id, "previous": previous}))
    adapter.tasks[task_id]["name"] = "partial-update"
    fresh = ScheduleManager(manager.config, adapter=adapter, clock=lambda: now[0])
    assert fresh.list()["tasks"][0]["name"] == "工作日资料"
    assert adapter.tasks[task_id]["generation"] == generation


def test_failure_result_never_persists_secrets_and_claim_survives_restart(setup):
    manager, adapter, now = setup
    task_id, generation = saved(manager, adapter, now[0])
    now[0] += timedelta(minutes=2)
    def fail(*args, **kwargs):
        raise RuntimeError("password=private_token")
    assert manager.run_due(task_id, generation, starter=fail)["code"] == "scheduled_start_failed"
    assert "private_token" not in manager.path.read_text(encoding="utf-8")
    fresh = ScheduleManager(manager.config, adapter=adapter, clock=lambda: now[0])
    assert fresh.run_due(task_id, generation, starter=fail)["code"] == "schedule_already_fired"


def test_maintenance_gate_and_missed_task_never_start(setup):
    manager, adapter, now = setup
    task_id, generation = saved(manager, adapter, now[0])
    now[0] += timedelta(minutes=2)
    marker = manager.data / "upgrade-state.json"
    marker.touch()
    assert manager.run_due(task_id, generation)["code"] == "maintenance_active"
    marker.unlink()
    now[0] += timedelta(minutes=11)
    assert manager.run_due(task_id, generation)["code"] == "schedule_missed"


def test_weekly_server_local_time_and_next_occurrence(setup):
    manager, adapter, now = setup
    local = now[0].astimezone()
    target = local + timedelta(minutes=2)
    manager.save(request(now[0], schedule={"kind": "weekly", "time": target.strftime("%H:%M"), "weekdays": [target.isoweekday()]}))
    task_id = next(iter(adapter.tasks))
    assert datetime.fromisoformat(manager.list()["tasks"][0]["next_run_at"]) == target
    now[0] += timedelta(minutes=2)
    manager.run_due(task_id, adapter.tasks[task_id]["generation"], starter=lambda *a, **k: None)
    assert datetime.fromisoformat(manager.list()["tasks"][0]["next_run_at"]) == target + timedelta(days=7)


def test_corrupt_store_is_fail_closed(setup):
    manager, _, _ = setup
    manager.path.write_text('{"schema_version":1,"revision":1,"tasks":[{}]}')
    with pytest.raises(ScheduleError) as error:
        manager.list()
    assert error.value.code == "schedules_state_invalid"


def test_once_without_offset_is_resolved_on_server_not_browser(setup):
    manager, adapter, now = setup
    target = now[0].astimezone() + timedelta(minutes=2)
    value = manager.save(request(now[0], schedule={"kind": "once", "at": target.replace(tzinfo=None).isoformat()}))
    assert datetime.fromisoformat(value["tasks"][0]["schedule"]["at"]) == target


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="Process TZ control is POSIX-only")
def test_once_uses_future_dst_offset_and_rejects_nonexistent_wall_time(setup):
    manager, _, now = setup
    old_timezone = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        now[0] = datetime(2030, 1, 1, tzinfo=timezone.utc)
        value = manager.save(request(now[0], schedule={"kind": "once", "at": "2030-07-01T10:00:00"}))
        assert value["tasks"][0]["schedule"]["at"].endswith("-04:00")
        with pytest.raises(ScheduleError):
            manager.save(request(now[0], schedule={"kind": "once", "at": "2030-03-10T02:30:00"}))
    finally:
        if old_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_timezone
        time.tzset()


@pytest.mark.skipif(os.name != "nt", reason="Checks the actual Windows lock primitive")
def test_windows_launcher_refuses_loading_runtime_during_upgrade(setup, tmp_path):
    from data_search.service import InstanceLock
    manager, adapter, now = setup
    task_id, _ = saved(manager, adapter, now[0])
    output = tmp_path / "loaded.txt"
    probe = tmp_path / "probe.py"
    probe.write_text("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('loaded')", encoding="utf-8")
    native = SystemScheduler(manager.config, command=[sys.executable, str(probe), str(output)])
    script = native._launcher(adapter.tasks[task_id])
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    with InstanceLock(tmp_path / "upgrade.lock"):
        assert subprocess.run(command, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 75
    assert not output.exists()
    (tmp_path / "upgrade-state.json").touch()
    assert subprocess.run(command, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 75
    assert not output.exists()
    (tmp_path / "upgrade-state.json").unlink()
    assert subprocess.run(command, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
    assert output.read_text() == "loaded"


@pytest.mark.skipif(os.name != "nt", reason="Checks Windows COM missing-task exit status")
def test_windows_missing_task_delete_is_idempotent(setup):
    import uuid
    manager, _, _ = setup
    # No task is created; delete only an unguessable name in this fixture's namespace.
    SystemScheduler(manager.config).remove(uuid.uuid4().hex)


def test_windows_registration_contains_hidden_stable_launcher_and_gate(setup):
    manager, adapter, now = setup
    task_id, _ = saved(manager, adapter, now[0])
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='"test-user","S-1-5-21-12345-1001"\n')
    native = SystemScheduler(manager.config, runner=runner, platform="windows", command=[r"C:\folder with spaces\data-search.exe"])
    native.put(adapter.tasks[task_id])
    xml = (native.directory / (task_id + ".xml")).read_text()
    tree = ET.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert tree.find(".//t:LogonType", ns).text == "InteractiveToken"
    assert "-WindowStyle Hidden" in xml and "<StartWhenAvailable>false" in xml
    script = next(native.directory.glob("*.ps1")).read_text(encoding="utf-8-sig")
    assert script.index("$gate.Lock") < script.index("upgrade-state.json") < script.index("scheduled-start")
    assert "--generation" in script and "finally" in script
    assert any(command[0] == "schtasks.exe" for command in calls)


def test_linux_timer_uses_calendar_no_catchup_and_server_user_manager(setup, tmp_path):
    manager, adapter, now = setup
    task_id, _ = saved(manager, adapter, now[0])
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="")
    native = SystemScheduler(manager.config, runner=runner, platform="linux", command=["/safe/app/data-search"], unit_dir=tmp_path / "units")
    native.put(adapter.tasks[task_id])
    timer = next(native.unit_dir.glob("*.timer")).read_text()
    assert "2030-06-03 08:02:00 UTC" in timer and "Persistent=false" in timer
    script = next(native.directory.glob("*.sh")).read_text()
    assert script.index("flock -n 9") < script.index("upgrade-state.json") < script.index("scheduled-start")
    assert ["systemctl", "--user", "enable", "--now", native.prefix + task_id + ".timer"] in calls


def test_scheduler_unavailable_does_not_claim_success(setup):
    manager, _, now = setup
    def runner(*args, **kwargs):
        raise FileNotFoundError()
    native = SystemScheduler(manager.config, runner=runner, platform="linux")
    manager.adapter = native
    with pytest.raises(ScheduleError) as error:
        manager.save(request(now[0]))
    # Failed compensation remains journalled; callbacks fail closed until repaired.
    assert error.value.code == "schedule_recovery_required"
    assert manager.journal.exists()
