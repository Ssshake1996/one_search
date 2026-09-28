"""Resolve the current user's Documents folder without widening search scope."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import sys
import uuid


def _windows_documents() -> str:
    # Known folders honor OneDrive and other user-configured folder redirection.
    folder_id = (ctypes.c_byte * 16).from_buffer_copy(
        uuid.UUID('FDD39AD0-238F-46AF-ADB4-6C85480369C7').bytes_le)
    path = ctypes.c_void_p()
    shell = ctypes.windll.shell32.SHGetKnownFolderPath
    shell.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell.restype = ctypes.c_long
    free = ctypes.windll.ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    try:
        if shell(ctypes.byref(folder_id), 0, None, ctypes.byref(path)) != 0 or not path.value:
            raise OSError('Documents known folder is unavailable')
        return ctypes.wstring_at(path)
    finally:
        if path.value:
            free(path)


def _linux_documents(home: Path) -> str:
    config_home = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    if not config_home.is_absolute():
        config_home = home / '.config'
    try:
        lines = (config_home / 'user-dirs.dirs').read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return str(home / 'Documents')
    for line in lines:
        if re.match(r'^\s*XDG_DOCUMENTS_DIR\s*=', line):
            match = re.fullmatch(r'\s*XDG_DOCUMENTS_DIR\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?', line)
            if not match:
                raise ValueError('Invalid XDG_DOCUMENTS_DIR')
            raw = match[1]
            if raw.startswith('$HOME/'):
                raw = str(home) + raw[5:]
            elif raw == '$HOME':
                raw = str(home)
            # Decode only shell-quoted literals; never evaluate shell code.
            if re.search(r'(?<!\\)[$`]', raw):
                raise ValueError('Unsupported XDG_DOCUMENTS_DIR expansion')
            return re.sub(r'\\([\\"$`])', r'\1', raw)
    return str(home / 'Documents')


def documents_roots() -> list[str]:
    """Return one existing Documents directory, or an empty, safe selection."""
    try:
        home = Path.home().resolve()
        raw = _windows_documents() if sys.platform == 'win32' else _linux_documents(home)
        path = Path(raw)
        if not path.is_absolute():
            return []
        path = path.resolve()
        # XDG uses $HOME to disable a special directory. Neither that nor a
        # drive root is a safe implicit Documents selection on any platform.
        if path == home or path == Path(path.anchor) or not path.is_dir():
            return []
        return [str(path)]
    except (OSError, ValueError, RuntimeError):
        return []
