"""Discover local disks and enforce a bounded, explicit file-search scope.

Discovery never elevates permissions or follows remote mounts/reparse directories.
Old configurations retain their selected roots; only new machine configurations
discover volumes. The daemon refreshes discovery before each full scan.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import platform
import stat
import sys

import psutil

from .config import contained


LINUX_SPECIAL = ('/proc', '/sys', '/dev', '/run')
LOCAL_FILESYSTEMS = {
    'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'vfat', 'msdos', 'exfat',
    'ntfs', 'ntfs3', 'fuseblk', 'bcachefs', 'reiserfs', 'jfs', 'ufs', 'f2fs',
}
REMOTE_FILESYSTEMS = {'nfs', 'nfs4', 'cifs', 'smbfs', 'smb3', '9p', 'ceph', 'afs', 'glusterfs'}


def _windows_volumes() -> tuple[list[str], list[dict]]:
    kernel = ctypes.windll.kernel32
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise OSError('local_disk_discovery_failed')
    roots, skipped = [], []
    for index in range(26):
        if mask & (1 << index):
            root = chr(65 + index) + ':\\'
            kind = kernel.GetDriveTypeW(ctypes.c_wchar_p(root))
            if kind == 3:  # DRIVE_FIXED; mapped/network, removable and optical are excluded.
                roots.append(root)
            else:
                skipped.append({'path': root, 'reason': 'not_local_fixed_disk'})
    return roots, skipped


def _linux_volumes() -> tuple[list[str], list[dict]]:
    roots, skipped = [], []
    for partition in psutil.disk_partitions(all=True):
        mount = partition.mountpoint
        filesystem = partition.fstype.lower()
        # Container roots may be overlay. Unknown child mounts are conservatively
        # excluded so walking / cannot inadvertently enter remote or virtual data.
        local_root = mount == '/' and filesystem not in REMOTE_FILESYSTEMS and not filesystem.startswith('fuse.')
        if filesystem in LOCAL_FILESYSTEMS or local_root:
            roots.append(mount)
        else:
            skipped.append({'path': mount, 'reason': 'remote_or_virtual_filesystem', 'filesystem': filesystem})
    if not any(item == '/' for item in roots) and not any(item['path'] == '/' for item in skipped):
        raise OSError('root_mount_discovery_failed')
    return roots, skipped


def discover_volumes() -> tuple[list[str], list[dict]]:
    if platform.system() == 'Windows':
        return _windows_volumes()
    if platform.system() == 'Linux':
        return _linux_volumes()
    raise OSError('machine_scope_platform_unsupported; configure explicit directories')


def link_directory(path: Path) -> bool:
    """Python 3.11 has no Path.is_junction; reject Windows directory reparse points."""
    try:
        details = path.lstat()
        return stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
            and stat.S_ISDIR(details.st_mode))
    except OSError:
        return False


def _normalize(paths, *, resolve=True) -> list[Path]:
    return list(dict.fromkeys(Path(p).expanduser().resolve() if resolve else Path(os.path.abspath(p)) for p in paths))


def _minimal_roots(paths: list[Path]) -> list[Path]:
    roots = []
    for path in sorted(paths, key=lambda p: len(p.parts)):
        if not any(contained(path, parent) for parent in roots):
            roots.append(path)
    return roots


def program_paths() -> list[Path]:
    package = Path(__file__).resolve().parent
    # In a source checkout exclude the executable package, not sibling docs,
    # examples or benchmark corpora. Installers separately exclude their app root.
    paths = [package, Path(sys.prefix).resolve()]
    if getattr(sys, 'frozen', False):
        paths.append(Path(sys.executable).resolve().parent)
    return paths


class FileScope:
    def __init__(self, config: dict):
        self.mode = config.get('scope', 'directories')
        self.configured_roots = list(config['roots'])
        self.excluded_names = {name.casefold() for name in config['exclude_names']}
        self.skipped_volumes = []
        self.discovery_error = None
        if self.mode == 'machine':
            try:
                discovered, self.skipped_volumes = discover_volumes()
            except OSError as exc:
                discovered = []
                self.discovery_error = str(exc)[:200]
        else:
            discovered = config['roots']
        self.discovered_roots = _normalize(discovered)
        # Do not resolve/stat excluded network mounts: even inspecting a hung
        # mount can block. Mount-table paths are already absolute local names.
        skipped_paths = [item['path'] for item in self.skipped_volumes
                         if any(Path(os.path.abspath(item['path'])).is_relative_to(root) for root in self.discovered_roots)]
        self.exclusions = _normalize([
            config['data_dir'], config['semantic']['model_dir'],
            *program_paths(), *config.get('exclude_paths', []),
            *(LINUX_SPECIAL if self.mode == 'machine' and platform.system() == 'Linux' else []),
        ])
        self.exclusions.extend(_normalize(skipped_paths, resolve=False))
        available, self.unavailable_roots = [], []
        for root in self.discovered_roots:
            try:
                if link_directory(root):
                    raise OSError('link_root_skipped')
                with os.scandir(root):
                    pass
                available.append(root)
            except OSError as exc:
                self.unavailable_roots.append({'path': str(root), 'reason': type(exc).__name__})
        self.roots = _minimal_roots(available)

    def allowed(self, path: Path) -> bool:
        try:
            lexical = Path(os.path.abspath(path))
            if any(lexical.is_relative_to(excluded) for excluded in self.exclusions):
                return False
            resolved = path.resolve()
            # Resolve aliases before checking scope, but never serve a path that
            # has since become a symlink/junction alias inside an allowed root.
            if os.path.normcase(os.path.abspath(path)) != os.path.normcase(str(resolved)):
                return False
            if any(resolved.is_relative_to(excluded) for excluded in self.exclusions):
                return False
            for root in self.roots:
                try:
                    relative = resolved.relative_to(root)
                    if not any(part.casefold() in self.excluded_names for part in relative.parts):
                        return True
                except ValueError:
                    continue
        except (OSError, RuntimeError):
            pass
        return False

    def report(self) -> dict:
        return {
            'mode': self.mode, 'configured_roots': self.configured_roots,
            'effective_roots': [str(p) for p in self.roots],
            'discovered_roots': [str(p) for p in self.discovered_roots],
            'excluded_paths': [str(p) for p in self.exclusions],
            'excluded_names': sorted(self.excluded_names),
            'skipped_volumes': self.skipped_volumes,
            'unavailable_roots': self.unavailable_roots,
            'discovery_error': self.discovery_error,
        }
