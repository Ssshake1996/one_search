"""Best-effort OS controls for disposable workers, with observable fallbacks.

Windows Job Objects cap committed virtual memory per process, not RSS. CPU caps
are relative to an enclosing job's allocation if the host already imposes one.
Linux applies scheduling/affinity only; the daemon's RSS budget remains sampled.
"""
from __future__ import annotations

import ctypes
import os

import psutil


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', ctypes.c_uint32), ('minimum_working_set', ctypes.c_size_t),
                ('maximum_working_set', ctypes.c_size_t), ('active_process_limit', ctypes.c_uint32),
                ('affinity', ctypes.c_size_t), ('priority', ctypes.c_uint32), ('scheduling', ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('read_operations', 'write_operations', 'other_operations', 'read_bytes', 'write_bytes', 'other_bytes')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', _IoCounters),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


class _CpuLimits(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint32), ('rate', ctypes.c_uint32)]


def _kernel_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    specifications = {
        'CreateJobObjectW': ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
        'OpenProcess': ([ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
        'SetInformationJobObject': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        'QueryInformationJobObject': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_int),
        'AssignProcessToJobObject': ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        'CloseHandle': ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in specifications.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


class _WindowsJob:
    def __init__(self, pid: int, memory_mb: int, cpu_percent: int):
        self.kernel = _kernel_api()
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        self.cpu_error = None
        try:
            extended = _ExtendedLimits()
            extended.basic.flags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
            extended.process_memory = memory_mb * 1048576
            self._set(9, extended)
            # CPU rate control can be unavailable under host job/DFSS policy.
            # Keep a working memory cap rather than discarding both controls.
            try:
                self._set(15, _CpuLimits(0x1 | 0x4, cpu_percent * 100))
            except OSError as exc:
                self.cpu_error = _error('cpu_job', exc)
            process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.close()
            raise
        finally:
            if process:
                self.kernel.CloseHandle(process)

    def _set(self, kind, value):
        if not self.kernel.SetInformationJobObject(self.handle, kind, ctypes.byref(value), ctypes.sizeof(value)):
            raise ctypes.WinError(ctypes.get_last_error())

    def query(self) -> dict:
        """Read the applied settings from Windows, useful for validation."""
        values = {}
        for kind, structure, key in ((9, _ExtendedLimits, 'memory'), (15, _CpuLimits, 'cpu')):
            result = structure()
            if not self.kernel.QueryInformationJobObject(self.handle, kind, ctypes.byref(result), ctypes.sizeof(result), None):
                raise ctypes.WinError(ctypes.get_last_error())
            values[key] = result
        return {'memory_bytes': values['memory'].process_memory,
                'memory_flags': values['memory'].basic.flags,
                'cpu_rate': values['cpu'].rate, 'cpu_flags': values['cpu'].flags}

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _error(action, exc):
    code = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
    return f'{action}: {type(exc).__name__}' + (f' ({code})' if code else '')


class WorkerControl:
    def __init__(self, pid, config):
        self.job = None
        self.status = {'pid': pid, 'active': True, 'platform': os.name,
                       'priority': None, 'io_priority': None, 'affinity_cpus': [],
                       'hard_memory_limit_mb': None, 'hard_cpu_percent': None,
                       'memory_metric': None, 'fallback_errors': [],
                       'process_tree_rss_enforcement': 'sampled'}
        try:
            process = psutil.Process(pid)
        except psutil.Error as exc:
            self.status['fallback_errors'].append(_error('process', exc))
            return
        actions = [
            ('priority', lambda: process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10),
             lambda: self.status.update(priority='below_normal' if os.name == 'nt' else 'nice_10')),
            ('io_priority', lambda: process.ionice(psutil.IOPRIO_LOW if os.name == 'nt' else psutil.IOPRIO_CLASS_IDLE),
             lambda: self.status.update(io_priority='low' if os.name == 'nt' else 'idle')),
        ]
        for name, apply, record in actions:
            try:
                apply()
                record()
            except (psutil.Error, OSError, AttributeError, ValueError) as exc:
                self.status['fallback_errors'].append(_error(name, exc))
        try:
            eligible = process.cpu_affinity()
            selected = eligible[:max(1, min(config['semantic'].get('threads', 1), len(eligible)))]
            process.cpu_affinity(selected)
            self.status['affinity_cpus'] = selected
        except (psutil.Error, OSError, AttributeError, ValueError) as exc:
            self.status['fallback_errors'].append(_error('affinity', exc))
        if os.name == 'nt':
            resource = config['resource']
            memory = resource.get('worker_memory_mb', 512)
            cpu = resource.get('worker_cpu_percent', 25)
            try:
                self.job = _WindowsJob(pid, memory, cpu)
                self.status.update(hard_memory_limit_mb=memory, memory_metric='committed_virtual_memory',
                                   hard_cpu_percent=None if self.job.cpu_error else cpu,
                                   cpu_rate_basis='system_or_enclosing_job_allocation')
                if self.job.cpu_error:
                    self.status['fallback_errors'].append(self.job.cpu_error)
            except (OSError, AttributeError) as exc:
                self.status['fallback_errors'].append(_error('job_object', exc))
        else:
            self.status['fallback_errors'].append('hard_memory_and_cpu_caps_unavailable_on_this_platform')

    def close(self):
        if self.job:
            self.job.close()
            self.job = None
        self.status['active'] = False


def attach_worker(pid: int, config: dict) -> WorkerControl:
    return WorkerControl(pid, config)
