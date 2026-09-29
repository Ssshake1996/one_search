"""Real DSH Web/MCP lifecycle acceptance using isolated synthetic installations.

No LLM calls, default DSH profile edits or real user file scans. The one enabled
OS task belongs only to the synthetic fixture and is removed on completion.
Source mode uses this checkout's venv. --native-bundle copies its runtime into
the fresh fixture, permitting identical checks against a packaged executable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
spec = importlib.util.spec_from_file_location("upgrade_acceptance", HERE.parent / "v051/verify_upgrade_v051.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
support.QUERY = "servicefixture060"


class Acceptance:
    def __init__(self, args):
        self.work = args.work.resolve()
        assert self.work.is_relative_to(REPO / '.packaging-smoke') and self.work.name.startswith('service-v060-')
        assert not self.work.exists(), 'Use a fresh synthetic fixture directory'
        self.work.mkdir(parents=True)
        self.dsh = args.dsh.resolve()
        self.bundle = (args.bundle or (args.native_bundle / 'plugins/deepseek-harness' if args.native_bundle else REPO / 'plugins/deepseek-harness')).resolve()
        self.profile_worker = HERE / 'dsh_service_profile_v060.mjs'
        self.offline = args.offline
        self.profiles = []
        self.data = self.work / ('数据 space' if args.native_bundle else 'data')
        self.config = self.data / 'config.json'
        self.root = self.work / '合成检索资料'
        self.root.mkdir()
        (self.root / 'fixture-service.md').write_text('servicefixture060 synthetic lifecycle acceptance only', encoding='utf-8')
        if args.native_bundle:
            runtime = args.native_bundle.resolve() / 'runtime'
            assert (runtime / 'data-search.exe').is_file()
            shutil.copytree(runtime, self.work / '运行时 space')
            self.executable, self.command_args = self.work / '运行时 space/data-search.exe', []
        else:
            self.executable, self.command_args = REPO / '.venv/Scripts/python.exe', ['-m', 'data_search']
        self.report = {'schema_version': 1, 'tested_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': 'native' if args.native_bundle else 'source', 'fixture': str(self.work.relative_to(REPO)),
            'host_cli_version': json.loads((self.dsh / 'package.json').read_text())['version'],
            'synthetic_only': True, 'default_dsh_profile_modified': False, 'model_requests': 0,
            'enabled_os_tasks_created': 0, 'checks': {}, 'source_commit': support.run(['git', 'rev-parse', 'HEAD']).stdout.strip(),
            'runtime_source_commit': args.runtime_source_commit,
            'plugin_hashes': {name: hashlib.sha256((self.bundle / name).read_bytes()).hexdigest()
                for name in ('index.mjs', 'bootstrap.mjs', 'upgrade-coordinator.mjs', 'web-host.mjs', 'service-control.mjs')},
            'backend_source_hashes': {name: hashlib.sha256((REPO / 'src/data_search' / name).read_bytes()).hexdigest()
                for name in ('service.py', 'service_control.py', 'service_schedules.py', 'web_management.py', 'cli.py')},
            'runtime_sha256': hashlib.sha256(self.executable.read_bytes()).hexdigest()}
        self.cli('init', '--data-dir', self.data, '--root', self.root)
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['semantic']['enabled'] = False
        self.config.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')

    def cli(self, *arguments, check=True):
        result = support.run([self.executable, *self.command_args, *arguments, '--config', self.config], check=check)
        try:
            return json.loads(result.stdout)
        except ValueError:
            pass
        values = support.json_lines(result.stdout)
        assert values, f'No CLI JSON (exit={result.returncode})'
        return values[-1]

    def web(self, profile, action, params=None):
        deadline = time.monotonic() + 60
        while True:
            envelope = profile.web(action, params)
            if (action == 'schedules_get' and not envelope.get('ok') and
                    envelope.get('error', {}).get('code') == 'schedules_busy' and time.monotonic() < deadline):
                self.report['schedule_busy_read_retries'] = self.report.get('schedule_busy_read_retries', 0) + 1
                time.sleep(.25)
                continue
            assert envelope.get('ok'), {'action': action, 'error': envelope.get('error')}
            return envelope['result']

    def state(self, profile):
        return self.web(profile, 'status')

    def no_daemon(self):
        marker_path = self.data / 'service-process.json'
        if not marker_path.exists():
            return True
        marker = json.loads(marker_path.read_text())
        try:
            process = psutil.Process(marker['pid'])
            return abs(process.create_time() - marker['create_time']) > .01 or not process.is_running()
        except psutil.NoSuchProcess:
            return True

    def stopped(self, profiles, seconds=8):
        deadline = time.monotonic() + seconds
        samples = 0
        while time.monotonic() < deadline:
            for profile in profiles:
                state = self.state(profile)
                assert state['control']['desired_state'] == 'stopped', state
                assert state['reconnect']['state'] == 'stopped', state
                assert state['reconnect']['next_retry_at'] is None, state
                assert not profile.call('state')['mcp_tools']
                assert 'index' not in state
            assert self.no_daemon(), 'Stopped backend revived during status reads'
            samples += 1
            time.sleep(.5)
        return {'seconds': seconds, 'samples': samples, 'tools_unregistered': True, 'daemon_absent': True, 'retry_disabled': True}

    def wait_tools(self, profile):
        support.eventually(lambda: len(profile.call('state')['mcp_tools']) == 11, timeout=80)
        return support.eventually(profile.search, timeout=30)

    def kill_owned_daemon(self):
        marker = json.loads((self.data / 'service-process.json').read_text())
        assert marker['role'] == 'data_search_daemon'
        assert Path(marker['config_path']).resolve() == self.config
        process = psutil.Process(marker['pid'])
        assert abs(process.create_time() - marker['create_time']) < .01
        command = process.cmdline()
        assert 'daemon' in command and '--config' in command
        assert Path(command[command.index('--config') + 1]).resolve() == self.config
        descendants = process.children(recursive=True)
        process.kill()
        process.wait(timeout=15)
        for child in descendants:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(descendants, timeout=10)
        return {'killed_pid': marker['pid'], 'service_id': marker['service_id'], 'process_identity_verified': True}

    def run(self):
        print('Starting isolated real DSH profiles A and B', flush=True)
        a = support.Profile(self, 'a', self.bundle)
        self.wait_tools(a)
        b = support.Profile(self, 'b', self.bundle)
        self.wait_tools(b)
        self.report['checks']['initial_profiles'] = {'profiles': 2, 'tools_per_profile': 11, 'real_mcp_search': True}

        print('Stopping through authenticated DSH Web RPC', flush=True)
        stopped = self.web(a, 'service_stop')
        assert stopped['control']['desired_state'] == 'stopped'
        support.eventually(lambda: not a.call('state')['mcp_tools'] and not b.call('state')['mcp_tools'])
        self.report['checks']['normal_stop'] = self.stopped([a, b])
        b.stop()
        print('Opening a new DSH profile while the backend is actively stopped', flush=True)
        entrant = support.Profile(self, 'stopped-entrant', self.bundle)
        self.report['checks']['new_profile_after_stop'] = self.stopped([a, entrant])

        print('Starting through authenticated DSH Web RPC', flush=True)
        started = self.web(entrant, 'service_start')
        assert started['control']['desired_state'] == 'running'
        self.wait_tools(a)
        self.wait_tools(entrant)
        self.report['checks']['manual_restart'] = {'both_profiles_tools_restored': True, 'real_mcp_search': True}

        print('Killing only the verified synthetic daemon to exercise unexpected recovery', flush=True)
        killed = self.kill_owned_daemon()
        samples = []
        deadline = time.monotonic() + 90
        recovered = False
        while time.monotonic() < deadline:
            state = self.state(a)
            retry = state.get('reconnect', {})
            if retry.get('attempt') or retry.get('state') != 'ready':
                samples.append({'profile': 'a', 'observed_at': time.time(), **retry, 'service_status': state['service']['status']})
            other = self.state(entrant)
            other_retry = other.get('reconnect', {})
            if other_retry.get('attempt') or other_retry.get('state') != 'ready':
                samples.append({'profile': 'stopped-entrant', 'observed_at': time.time(), **other_retry, 'service_status': other['service']['status']})
            marker_path = self.data / 'service-process.json'
            marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
            if state['service']['status'] == 'running' and marker.get('service_id') and marker['service_id'] != killed['service_id'] and retry.get('state') == 'ready':
                recovered = True
                break
            time.sleep(.15)
        assert recovered, {'message': 'Unexpected daemon exit did not recover', 'samples': samples[-12:]}
        self.wait_tools(a)
        self.wait_tools(entrant)
        assert any(sample.get('state') == 'waiting' and sample.get('attempt', 0) >= 1 for sample in samples), samples
        self.report['checks']['unexpected_exit'] = {**killed, 'recovered_automatically': True,
            'real_mcp_search': True, 'observed_reconnect_samples': samples,
            'scope': 'Real first retry and recovery; full delay progression and jitter bounds are covered by coordinator tests'}

        print('Forcing stop through DSH Web and verifying both hosts remain stopped', flush=True)
        forced = self.web(a, 'service_force_stop')
        assert forced['control']['reason'] == 'forced'
        support.eventually(lambda: not a.call('state')['mcp_tools'] and not entrant.call('state')['mcp_tools'])
        self.report['checks']['forced_stop'] = self.stopped([a, entrant], seconds=12)

        print('Exercising disabled schedule CRUD while the daemon remains stopped', flush=True)
        initial = self.web(a, 'schedules_get')
        assert initial['tasks'] == []
        snapshot = self.web(a, 'schedule_save', {'revision': initial['revision'], 'task': {
            'name': 'Synthetic disabled schedule', 'enabled': False, 'schedule': {'kind': 'daily', 'time': '09:00'}}})
        assert len(snapshot['tasks']) == 1 and not snapshot['tasks'][0]['enabled']
        task_id = snapshot['tasks'][0]['id']
        snapshot = self.web(entrant, 'schedule_save', {'revision': snapshot['revision'], 'task': {
            'id': task_id, 'name': 'Synthetic disabled weekly schedule', 'enabled': False,
            'schedule': {'kind': 'weekly', 'time': '10:00', 'weekdays': [1, 3]}}})
        assert snapshot['tasks'][0]['schedule']['kind'] == 'weekly'
        snapshot = self.web(a, 'schedule_delete', {'revision': snapshot['revision'], 'id': task_id})
        assert snapshot['tasks'] == []
        self.report['checks']['offline_schedule_crud'] = {'created': True, 'edited_from_second_profile': True,
            'deleted': True, 'tasks_left': 0, 'enabled_tasks_created': 0, 'daemon_absent': self.no_daemon()}
        self.report['checks']['after_schedule_crud'] = self.stopped([a, entrant], seconds=3)
        print('Waiting for a real enabled Windows once task to restart the forcibly stopped backend', flush=True)
        due = datetime.now().astimezone() + timedelta(seconds=30)
        snapshot = self.web(a, 'schedule_save', {'revision': snapshot['revision'], 'task': {
            'name': 'Synthetic real once trigger', 'enabled': True,
            'schedule': {'kind': 'once', 'at': due.isoformat(timespec='seconds')}}})
        self.report['enabled_os_tasks_created'] = 1
        task_id = snapshot['tasks'][0]['id']
        assert self.state(a)['control']['desired_state'] == 'stopped'
        fired = None
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            current = self.web(a, 'schedules_get')
            task = next(item for item in current['tasks'] if item['id'] == task_id)
            result = task.get('last_result') or {}
            if result.get('status') == 'failed':
                raise AssertionError({'scheduled_start': result})
            if result.get('status') == 'started':
                fired = task
                break
            time.sleep(.5)
        assert fired, 'Operating system once task did not report a successful start'
        assert self.state(a)['control']['reason'] == 'scheduled'
        self.wait_tools(a)
        self.wait_tools(entrant)
        latest = self.web(a, 'schedules_get')
        deleted = self.web(a, 'schedule_delete', {'revision': latest['revision'], 'id': task_id})
        assert deleted['tasks'] == []
        self.report['checks']['real_os_once_trigger'] = {'scheduler': snapshot['scheduler']['kind'],
            'requested_at': due.isoformat(timespec='seconds'), 'last_run_at': fired['last_run_at'],
            'last_result': fired['last_result'], 'control_reason': 'scheduled', 'both_profiles_tools_restored': True,
            'real_mcp_search': True, 'task_deleted': True, 'direct_scheduled_start_command_used_by_harness': False}
        self.report['passed'] = True
        self.report['unverified'] = ['No LLM/model request or real user data is included',
            'Task validation, weekly calendar timing and once-task timezone conversion are covered separately by scheduler tests',
            'This is Windows Task Scheduler acceptance; real Linux systemd timer execution is not exercised here']

    def cleanup(self):
        try:
            deadline = time.monotonic() + 60
            while True:
                cleared = self.cli('schedules-clear', check=False)
                if cleared.get('error', {}).get('code') in {'schedules_busy', 'schedule_maintenance_busy'} and time.monotonic() < deadline:
                    time.sleep(.25)
                    continue
                assert cleared.get('tasks') == [], cleared
                break
        except Exception as error:
            self.report.setdefault('cleanup_errors', []).append('schedules:' + type(error).__name__)
        for profile in reversed(self.profiles):
            try:
                profile.stop()
            except Exception as error:
                self.report.setdefault('cleanup_errors', []).append(type(error).__name__)
        try:
            self.cli('force-stop')
            assert self.no_daemon()
        except Exception as error:
            self.report.setdefault('cleanup_errors', []).append(type(error).__name__)
        self.report['cleaned_up'] = not self.report.get('cleanup_errors')
        (self.work / 'report.json').write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--dsh', type=Path, required=True)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--native-bundle', type=Path)
    parser.add_argument('--runtime-source-commit', help='Exact source commit used to build the native candidate, when known')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    acceptance = Acceptance(args)
    try:
        acceptance.run()
    except Exception as error:
        acceptance.report['passed'] = False
        acceptance.report['failure'] = {'type': type(error).__name__, 'message': str(error)[-5000:]}
        raise
    finally:
        acceptance.cleanup()
        print(json.dumps({'passed': acceptance.report.get('passed'), 'cleaned_up': acceptance.report.get('cleaned_up'),
            'report': str(acceptance.work / 'report.json')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
