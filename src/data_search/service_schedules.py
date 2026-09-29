"""Small, durable schedules backed by the current user's OS scheduler.

No resident scheduler thread is required. OS callbacks are generation checked,
claimed durably before starting, and enter through a launcher outside the runtime.
"""
from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from .service import InstanceLock, ServiceError

MAX_TASKS = 32
ID = re.compile(r"^[a-f0-9]{32}$")
TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
MAX_LATENESS = 600


class ScheduleError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone().isoformat(timespec="seconds")


def _date(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("An explicit timezone offset is required")
    return result


def _validate(request, now, *, existing=False):
    if not isinstance(request, dict) or set(request) - {"id", "name", "enabled", "schedule"}:
        raise ScheduleError("schedule_invalid", "定时任务包含未知字段。")
    name, enabled, schedule = request.get("name"), request.get("enabled"), request.get("schedule")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
        raise ScheduleError("schedule_invalid", "任务名称须为 1–80 个可显示字符。")
    if type(enabled) is not bool or not isinstance(schedule, dict):
        raise ScheduleError("schedule_invalid", "任务启用状态或时间格式无效。")
    kind = schedule.get("kind")
    if kind == "once":
        try:
            if (set(schedule) != {"kind", "at"} or not isinstance(schedule["at"], str) or len(schedule["at"]) > 40 or
                    not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", schedule["at"])):
                raise ValueError()
            at = datetime.fromisoformat(schedule["at"])
            if at.tzinfo is None:
                local = at.astimezone()
                if local.replace(tzinfo=None) != at:
                    raise ValueError()  # A spring-forward wall time does not exist.
                at = local
            if enabled and not existing and at <= now:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ScheduleError("schedule_invalid", "单次任务须指定有效的未来时间；未注明时区时按服务器本地时间解析。") from None
        schedule = {"kind": kind, "at": at.isoformat(timespec="seconds")}
    elif kind in {"daily", "weekly"}:
        keys = {"kind", "time", "weekdays"} if kind == "weekly" else {"kind", "time"}
        if set(schedule) != keys or not isinstance(schedule.get("time"), str) or not TIME.fullmatch(schedule["time"]):
            raise ScheduleError("schedule_invalid", "每日或每周任务时间须为 HH:MM。")
        schedule = dict(schedule)
        if kind == "weekly":
            days = schedule["weekdays"]
            if (not isinstance(days, list) or not 1 <= len(days) <= 7 or
                    any(type(day) is not int or not 1 <= day <= 7 for day in days) or len(set(days)) != len(days)):
                raise ScheduleError("schedule_invalid", "星期须为不重复的 1–7，1 表示星期一。")
            schedule["weekdays"] = sorted(days)
    else:
        raise ScheduleError("schedule_invalid", "仅支持单次、每日和每周启动任务。")
    return {"name": name.strip(), "enabled": enabled, "schedule": schedule}


def _occurrences(task, now):
    schedule = task["schedule"]
    if schedule["kind"] == "once":
        return [_date(schedule["at"])]
    hour, minute = map(int, schedule["time"].split(":"))
    day = now.astimezone().date()
    result = []
    for shift in range(-8, 9):
        date = day + timedelta(days=shift)
        if schedule["kind"] == "weekly" and date.isoweekday() not in schedule["weekdays"]:
            continue
        # Localize each calendar date separately, so OS daylight-saving rules apply.
        wall = datetime(date.year, date.month, date.day, hour, minute)
        candidate = wall.astimezone()
        if candidate.replace(tzinfo=None) != wall:
            continue
        if candidate >= _date(task["valid_after"]):
            result.append(candidate)
    return result


def _next(task, now):
    if not task["enabled"]:
        return None
    after = _date(task["last_occurrence"]) if task.get("last_occurrence") else None
    values = [at for at in _occurrences(task, now) if at > now and (after is None or at > after)]
    return _iso(min(values)) if values else None


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path, value):
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _command(config):
    """Use the installed CLI when frozen; source checkouts use this interpreter."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).resolve()), "-m", "data_search"]


class SystemScheduler:
    def __init__(self, config, *, runner=subprocess.run, platform=None, command=None, unit_dir=None):
        self.config = config
        self.data = Path(config["data_dir"]).resolve()
        self.directory = self.data / "service-schedules"
        self.platform = platform or ("windows" if os.name == "nt" else sys.platform)
        self.kind = "windows-task-scheduler" if self.platform == "windows" else "systemd-user" if self.platform == "linux" else "unsupported"
        self.runner = runner
        self.command = command or _command(config)
        self.unit_dir = Path(unit_dir or Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user")
        key = str(self.data).lower() if self.platform == "windows" else str(self.data)
        self.prefix = "one-search-" + hashlib.sha256(key.encode()).hexdigest()[:12] + "-"

    def _run(self, args, *, check=True):
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=25,
                                 **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        except (OSError, subprocess.SubprocessError):
            raise ScheduleError("scheduler_unavailable", "无法访问系统任务调度器；请检查当前用户会话、组件和权限。") from None
        if check and result.returncode:
            raise ScheduleError("scheduler_rejected", "系统任务调度器拒绝操作；请检查当前用户的任务权限和用户服务状态。")
        return result

    def _check(self):
        if self.kind == "unsupported":
            raise ScheduleError("scheduler_unsupported", "此系统尚不支持定时启动；支持 Windows 任务计划程序和 Linux systemd 用户定时器。")
        if self.kind == "systemd-user":
            self._run(["systemctl", "--user", "show-environment"])
            self._run(["flock", "--version"])

    def _launcher(self, task):
        arguments = self.command + ["scheduled-start", "--schedule-id", task["id"], "--generation", task["generation"],
                                    "--config", str(Path(self.config["config_path"]).resolve())]
        if any(not isinstance(arg, str) or any(c in arg for c in "\r\n\0") for arg in arguments):
            raise ScheduleError("schedule_command_invalid", "安装路径含有不受支持的控制字符。")
        extension = ".ps1" if self.platform == "windows" else ".sh"
        path = self.directory / (task["id"] + "-" + task["generation"] + extension)
        if self.platform == "windows":
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            text = ("$ErrorActionPreference = 'Stop'\n$gate = $null\n$locked = $false\ntry {\n"
                    "  $gate = [IO.File]::Open(" + quote(self.data / "upgrade.lock") + ", 'OpenOrCreate', 'ReadWrite', 'ReadWrite')\n"
                    "  $gate.Lock(0, 1)\n  $locked = $true\n"
                    "  if (Test-Path -LiteralPath " + quote(self.data / "upgrade-state.json") + ") { exit 75 }\n"
                    "  $null = & " + " ".join(quote(arg) for arg in arguments) + "\n  exit $LASTEXITCODE\n"
                    "} catch { exit 75 } finally {\n  if ($gate) { if ($locked) { $gate.Unlock(0, 1) }; $gate.Dispose() }\n}\n")
            # Windows PowerShell 5.1 needs a BOM for non-ASCII installation paths.
            text = "\ufeff" + text
        else:
            text = ("#!/bin/sh\nset -eu\nexec 9>" + shlex.quote(str(self.data / "upgrade.lock")) + "\n"
                    "flock -n 9 || exit 75\n[ ! -e " + shlex.quote(str(self.data / "upgrade-state.json")) + " ] || exit 75\n"
                    + " ".join(shlex.quote(arg) for arg in arguments) + "\n")
        _write_text(path, text)
        return path

    def _windows_xml(self, task, launcher, sid):
        ET.register_namespace("", "http://schemas.microsoft.com/windows/2004/02/mit/task")
        root = ET.Element("Task", {"version": "1.2", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"})
        def element(parent, key, value=None, **attrs):
            node = ET.SubElement(parent, key, attrs)
            if value is not None:
                node.text = str(value)
            return node
        info = element(root, "RegistrationInfo")
        element(info, "Description", "one_search scheduled service start")
        triggers = element(root, "Triggers")
        schedule = task["schedule"]
        trigger = element(triggers, "TimeTrigger" if schedule["kind"] == "once" else "CalendarTrigger")
        if schedule["kind"] == "once":
            boundary = schedule["at"]
        else:
            date = _date(task["valid_after"]).astimezone().date().isoformat()
            boundary = date + "T" + schedule["time"] + ":00"
        element(trigger, "StartBoundary", boundary)
        element(trigger, "Enabled", str(task["enabled"]).lower())
        if schedule["kind"] == "daily":
            element(element(trigger, "ScheduleByDay"), "DaysInterval", 1)
        elif schedule["kind"] == "weekly":
            weekly = element(trigger, "ScheduleByWeek")
            element(weekly, "WeeksInterval", 1)
            days = element(weekly, "DaysOfWeek")
            for day in schedule["weekdays"]:
                element(days, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][day - 1])
        principal = element(element(root, "Principals"), "Principal", id="Author")
        element(principal, "UserId", sid)
        element(principal, "LogonType", "InteractiveToken")
        element(principal, "RunLevel", "LeastPrivilege")
        settings = element(root, "Settings")
        for key, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                           ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "false"),
                           ("Enabled", str(task["enabled"]).lower()), ("Hidden", "true"),
                           ("ExecutionTimeLimit", "PT2M")):
            element(settings, key, value)
        action = element(element(root, "Actions", Context="Author"), "Exec")
        element(action, "Command", "powershell.exe")
        element(action, "Arguments", subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-ExecutionPolicy", "Bypass", "-File", str(launcher)]))
        return ET.tostring(root, encoding="unicode")

    def put(self, task):
        self._check()
        launcher = self._launcher(task)
        name = self.prefix + task["id"]
        if self.platform == "windows":
            try:
                sid = next(csv.reader(self._run(["whoami", "/user", "/fo", "csv", "/nh"]).stdout.strip().splitlines()))[1]
                if not re.fullmatch(r"S-1-(?:\d+-)+\d+", sid):
                    raise ValueError()
            except (ValueError, IndexError, StopIteration):
                raise ScheduleError("scheduler_identity_invalid", "无法确定当前 Windows 用户身份。") from None
            xml = self.directory / (task["id"] + ".xml")
            _write_text(xml, self._windows_xml(task, launcher, sid))
            self._run(["schtasks.exe", "/Create", "/TN", name, "/XML", str(xml), "/F"])
        else:
            def quote(value):
                return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
            schedule = task["schedule"]
            if schedule["kind"] == "once":
                calendar = _date(schedule["at"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                days = "" if schedule["kind"] == "daily" else ",".join(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i - 1] for i in schedule["weekdays"]) + " "
                calendar = days + "*-*-* " + schedule["time"] + ":00"
            _write_text(self.unit_dir / (name + ".service"), "[Unit]\nDescription=one_search scheduled start\n[Service]\nType=oneshot\nUMask=0077\nExecStart=/bin/sh " + quote(launcher) + "\nTimeoutStartSec=120\n")
            _write_text(self.unit_dir / (name + ".timer"), "[Unit]\nDescription=one_search start timer\n[Timer]\nOnCalendar=" + calendar + "\nAccuracySec=1s\nPersistent=false\nUnit=" + name + ".service\n[Install]\nWantedBy=timers.target\n")
            self._run(["systemctl", "--user", "daemon-reload"])
            self._run(["systemctl", "--user", "enable" if task["enabled"] else "disable", "--now", name + ".timer"])
            # A changed trigger must replace the running timer's previous calendar.
            if task["enabled"]:
                self._run(["systemctl", "--user", "restart", name + ".timer"])

    def remove(self, task_id):
        self._check()
        name = self.prefix + task_id
        if self.platform == "windows":
            # COM gives a stable missing-task HRESULT, independent of OS language.
            script = "$ErrorActionPreference='Stop'; $s=New-Object -ComObject Schedule.Service; $s.Connect(); try { $s.GetFolder('\\').DeleteTask('" + name + "',0) } catch { $e=$_.Exception; while ($e.InnerException) { $e=$e.InnerException }; if ($e.HResult -ne -2147024894) { exit 1 } }; exit 0"
            self._run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script])
        else:
            timer = self.unit_dir / (name + ".timer")
            if timer.exists():
                self._run(["systemctl", "--user", "disable", "--now", name + ".timer"])
            for suffix in (".timer", ".service"):
                (self.unit_dir / (name + suffix)).unlink(missing_ok=True)
            self._run(["systemctl", "--user", "daemon-reload"])

    def cleanup(self, task_id, generation=None):
        if not self.directory.exists():
            return
        for path in self.directory.glob(task_id + "-*"):
            if generation is None or path.stem != task_id + "-" + generation:
                path.unlink(missing_ok=True)
        if generation is None:
            (self.directory / (task_id + ".xml")).unlink(missing_ok=True)


class ScheduleManager:
    def __init__(self, config, *, adapter=None, clock=_now):
        self.config, self.clock = config, clock
        self.data = Path(config["data_dir"]).resolve()
        self.path = self.data / "service-schedules.json"
        self.journal = self.data / "service-schedules-pending.json"
        self.adapter = adapter or SystemScheduler(config)

    @contextmanager
    def _lock(self, *, wait=0):
        deadline = time.monotonic() + wait
        lock = InstanceLock(self.data / "service-schedules.lock")
        while True:
            try:
                lock.__enter__()
                break
            except ServiceError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    @contextmanager
    def _admission(self):
        gate = InstanceLock(self.data / "upgrade.lock")
        try:
            gate.__enter__()
        except ServiceError:
            raise ScheduleError("schedule_maintenance_busy", "正在升级、卸载或执行定时启动，暂时不能修改任务。") from None
        try:
            if (self.data / "upgrade-state.json").exists():
                raise ScheduleError("schedule_maintenance_busy", "维护尚未完成，暂时不能修改任务。")
            yield
        finally:
            gate.__exit__(None, None, None)

    def _validate_state(self, state):
        if (not isinstance(state, dict) or state.get("schema_version") != 1 or type(state.get("revision")) is not int or
                state["revision"] < 0 or not isinstance(state.get("tasks"), list) or len(state["tasks"]) > MAX_TASKS):
            raise ValueError()
        ids = set()
        for task in state["tasks"]:
            if not ID.fullmatch(task["id"]) or not ID.fullmatch(task["generation"]) or task["id"] in ids:
                raise ValueError()
            _validate({key: task[key] for key in ("name", "enabled", "schedule")}, self.clock(), existing=True)
            if task["schedule"]["kind"] == "once":
                _date(task["schedule"]["at"])
            _date(task["valid_after"])
            for key in ("last_run_at", "last_occurrence"):
                if task.get(key) is not None:
                    _date(task[key])
            ids.add(task["id"])
        return state

    def _read(self):
        if not self.path.exists():
            return {"schema_version": 1, "revision": 0, "tasks": []}
        try:
            if self.path.stat().st_size > 262144:
                raise ValueError()
            return self._validate_state(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError, ScheduleError):
            raise ScheduleError("schedules_state_invalid", "定时任务记录无法读取或已损坏；请保留记录并修复后重试。") from None

    def _recover(self):
        if not self.journal.exists():
            return
        try:
            if self.journal.stat().st_size > 524288:
                raise ValueError()
            record = json.loads(self.journal.read_text(encoding="utf-8"))
            task_ids = record.get("ids", [record.get("id")])
            if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= MAX_TASKS or
                    any(not isinstance(task_id, str) or not ID.fullmatch(task_id) for task_id in task_ids) or
                    len(set(task_ids)) != len(task_ids)):
                raise ValueError()
            previous = self._validate_state(record["previous"])
            for task_id in task_ids:
                old = next((task for task in previous["tasks"] if task["id"] == task_id), None)
                if old:
                    self.adapter.put(old)
                else:
                    self.adapter.remove(task_id)
            atomic_json(self.path, previous)
            self.journal.unlink()
        except (OSError, ValueError, KeyError, TypeError, ScheduleError):
            raise ScheduleError("schedule_recovery_required", "上次任务变更未完成，系统任务回滚失败；请恢复调度器权限后重试。") from None

    def _snapshot(self, state):
        now = self.clock()
        local = now.astimezone()
        offset = local.strftime("%z")
        tasks = [{key: task.get(key) for key in ("id", "name", "enabled", "schedule", "last_run_at", "last_result")} |
                 {"next_run_at": _next(task, now)} for task in state["tasks"]]
        for task in tasks:
            if task["schedule"]["kind"] == "once":
                task["schedule"] = {"kind": "once", "at": _iso(_date(task["schedule"]["at"]))}
        note = ("使用当前用户的系统任务；Windows 需该用户已登录，Linux 需 systemd 用户管理器保持运行。关闭网页或 DSH 不影响已注册任务；关机期间错过的时间不补跑。")
        return {"revision": state["revision"], "tasks": tasks,
                "timezone": {"name": local.tzname(), "offset": offset[:3] + ":" + offset[3:], "now": local.isoformat(timespec="seconds")},
                "scheduler": {"kind": self.adapter.kind, "note": note}}

    def list(self):
        try:
            with self._lock():
                if self.journal.exists():
                    # A pending transaction can write OS tasks during recovery.
                    # Nonblocking admission avoids lock-order deadlocks with CRUD.
                    with self._admission():
                        self._recover()
                return self._snapshot(self._read())
        except ServiceError:
            raise ScheduleError("schedules_busy", "定时任务正在变更或执行，请稍后重试。") from None

    def _commit(self, old, updated, task_id):
        atomic_json(self.journal, {"id": task_id, "previous": old})
        task = next((task for task in updated["tasks"] if task["id"] == task_id), None)
        try:
            if task:
                self.adapter.put(task)
            else:
                self.adapter.remove(task_id)
            atomic_json(self.path, updated)
            self.journal.unlink()
        except Exception:
            self._recover()
            raise
        try:
            self.adapter.cleanup(task_id, task["generation"] if task else None)
        except OSError:
            pass  # An old script held by a scanner cannot undo an already committed task.
        return self._snapshot(updated)

    def save(self, request, *, revision=None):
        now = self.clock()
        validated = _validate(request, now)
        try:
            with self._admission(), self._lock():
                self._recover()
                state = self._read()
                self._revision(state, revision)
                task_id = request.get("id")
                previous = next((task for task in state["tasks"] if task["id"] == task_id), None)
                if task_id is not None and previous is None:
                    raise ScheduleError("schedule_not_found", "任务不存在，请刷新任务列表。")
                if previous is None and len(state["tasks"]) >= MAX_TASKS:
                    raise ScheduleError("schedule_limit", "最多支持 32 个定时启动任务。")
                task_id = task_id or uuid.uuid4().hex
                task = {**validated, "id": task_id, "generation": uuid.uuid4().hex, "valid_after": now.isoformat(),
                        "last_run_at": previous.get("last_run_at") if previous else None,
                        "last_result": previous.get("last_result") if previous else None, "last_occurrence": None}
                updated = {**state, "revision": state["revision"] + 1,
                           "tasks": [item for item in state["tasks"] if item["id"] != task_id] + [task]}
                return self._commit(state, updated, task_id)
        except ServiceError:
            raise ScheduleError("schedules_busy", "定时任务正在变更或执行，请稍后重试。") from None

    @staticmethod
    def _revision(state, revision):
        if revision is not None and (type(revision) is not int or revision != state["revision"]):
            raise ScheduleError("schedule_conflict", "任务列表已更新，请刷新后再修改。")

    def delete(self, task_id, *, revision=None):
        try:
            with self._admission(), self._lock():
                self._recover()
                state = self._read()
                self._revision(state, revision)
                if not any(task["id"] == task_id for task in state["tasks"]):
                    raise ScheduleError("schedule_not_found", "任务不存在，请刷新任务列表。")
                updated = {**state, "revision": state["revision"] + 1,
                           "tasks": [task for task in state["tasks"] if task["id"] != task_id]}
                return self._commit(state, updated, task_id)
        except ServiceError:
            raise ScheduleError("schedules_busy", "定时任务正在变更或执行，请稍后重试。") from None

    def clear(self):
        """Remove this instance's tasks transactionally before uninstalling it.

        The uninstaller must hold upgrade.lock across this call and file removal,
        so an already queued OS launcher cannot load the runtime in between.
        """
        try:
            with self._lock():
                self._recover()
                state = self._read()
                task_ids = [task["id"] for task in state["tasks"]]
                if not task_ids:
                    return self._snapshot(state)
                atomic_json(self.journal, {"ids": task_ids, "previous": state})
                updated = {**state, "revision": state["revision"] + 1, "tasks": []}
                try:
                    for task_id in task_ids:
                        self.adapter.remove(task_id)
                    atomic_json(self.path, updated)
                    self.journal.unlink()
                except Exception:
                    self._recover()
                    raise
                for task_id in task_ids:
                    try:
                        self.adapter.cleanup(task_id)
                    except OSError:
                        pass
                return self._snapshot(updated)
        except ServiceError:
            raise ScheduleError("schedules_busy", "定时任务正在变更或执行，请稍后重试。") from None

    def run_due(self, task_id, generation, *, starter=None):
        def skipped(code):
            return {"status": "skipped", "schedule_id": task_id, "code": code}
        if not isinstance(task_id, str) or not ID.fullmatch(task_id) or not isinstance(generation, str) or not ID.fullmatch(generation):
            return skipped("schedule_not_found")
        try:
            with self._lock(wait=40):
                if self.journal.exists():
                    return skipped("schedule_recovery_required")
                state = self._read()
                task = next((task for task in state["tasks"] if task["id"] == task_id), None)
                if task is None or task["generation"] != generation or not task["enabled"]:
                    return skipped("schedule_inactive")
                now = self.clock()
                due = [at for at in _occurrences(task, now) if at <= now]
                if not due:
                    return skipped("schedule_not_due")
                at = max(due)
                if (now - at).total_seconds() > MAX_LATENESS:
                    return skipped("schedule_missed")
                if task.get("last_occurrence") and at <= _date(task["last_occurrence"]):
                    return skipped("schedule_already_fired")
                if (self.data / "upgrade-state.json").exists():
                    return skipped("maintenance_active")
                task["last_occurrence"], task["last_run_at"] = at.isoformat(), _iso(now)
                task["last_result"] = {"status": "triggered", "code": "start_unconfirmed"}
                atomic_json(self.path, state)  # A killed callback cannot execute this occurrence twice.
                if starter is None:
                    from .service_control import start
                    starter = start
                try:
                    starter(self.config, reason="scheduled")
                    task["last_result"] = {"status": "started"}
                    result = {"status": "started", "schedule_id": task_id}
                except Exception as error:
                    # Never persist raw exception strings, connection URLs or credentials.
                    code = getattr(error, "code", "scheduled_start_failed")
                    if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                        code = "scheduled_start_failed"
                    task["last_result"] = {"status": "failed", "code": code}
                    result = {"status": "failed", "schedule_id": task_id, "code": code}
                atomic_json(self.path, state)
                return result
        except ServiceError:
            return skipped("schedules_busy")


def list_schedules(config):
    return ScheduleManager(config).list()


def save_schedule(config, request, *, revision=None):
    return ScheduleManager(config).save(request, revision=revision)


def delete_schedule(config, task_id, *, revision=None):
    return ScheduleManager(config).delete(task_id, revision=revision)


def clear_schedules(config):
    return ScheduleManager(config).clear()


def run_due(config, schedule_id, generation):
    return ScheduleManager(config).run_due(schedule_id, generation)
