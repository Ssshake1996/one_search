"""Current-user OS credential storage. No plaintext fallback or credential export."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import shutil
import subprocess
import sys
from uuid import uuid4


class CredentialError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_REFERENCE = re.compile(r"one-search-vault:(windows|secret-service):([a-f0-9]{32})\Z")


def _backend() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "secret-service"
    raise CredentialError("vault_unavailable", "此系统未提供已支持的系统凭据库；可显式配置密码环境变量。")


def _parse(reference: str) -> tuple[str, str]:
    match = _REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
    if not match:
        raise CredentialError("invalid_credential_reference", "凭据引用格式无效，请重新保存密码。")
    backend, identity = match.groups()
    if backend != _backend():
        raise CredentialError("credential_platform_mismatch", "此凭据属于另一操作系统，请在当前账号下重新保存密码。")
    return backend, identity


def validate_reference(reference: str) -> None:
    """Validate a portable config reference without reading or unlocking its secret."""
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise CredentialError("invalid_credential_reference", "凭据引用格式无效，请重新保存密码。")


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _windows_api():
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _windows_error():
    if ctypes.get_last_error() == 1168:
        return CredentialError("credential_missing", "当前账号找不到保存的凭据，请重新保存密码。")
    return CredentialError("vault_locked_or_unavailable", "当前登录会话无法使用系统凭据库，请检查运行账号或重新登录。")


def _windows(operation: str, identity: str, secret: str | None = None):
    api, target = _windows_api(), "one-search/database/" + identity
    if operation == "store":
        encoded = secret.encode("utf-16-le")
        if len(encoded) > 2560:
            raise CredentialError("credential_too_large", "密码超过系统凭据库允许的长度。")
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = _Credential(Type=1, TargetName=target, Comment="one_search database credential",
            CredentialBlobSize=len(encoded), CredentialBlob=blob, Persist=2, UserName="one_search")
        try:
            if not api.CredWriteW(ctypes.byref(credential), 0):
                raise _windows_error()
        finally:
            ctypes.memset(blob, 0, len(encoded))
        return None
    if operation == "delete":
        if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
            raise _windows_error()
        return None
    pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise _windows_error()
    try:
        record = pointer.contents
        if record.CredentialBlobSize > 2560 or record.CredentialBlobSize % 2:
            raise CredentialError("invalid_credential", "保存的凭据无效，请重新保存密码。")
        try:
            return ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize).decode("utf-16-le")
        except UnicodeError:
            raise CredentialError("invalid_credential", "保存的凭据无效，请重新保存密码。") from None
    finally:
        api.CredFree(pointer)


def _secret_service(operation: str, identity: str, secret: str | None = None):
    executable = shutil.which("secret-tool")
    if not executable:
        raise CredentialError("vault_unavailable", "未找到系统 Secret Service 客户端 secret-tool；请安装并解锁系统凭据库，或显式使用环境变量。")
    command = [executable, {"store": "store", "read": "lookup", "delete": "clear"}[operation]]
    if operation == "store":
        command.append("--label=one_search database credential")
    command += ["application", "one_search", "kind", "database", "id", identity]
    try:
        # secret-tool accepts the exact stdin bytes through EOF, with no added newline.
        result = subprocess.run(command, input=secret.encode("utf-8") if operation == "store" else b"",
            capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CredentialError("vault_locked_or_unavailable", "系统凭据库不可用或未解锁，请在相同登录会话中解锁后重试。") from None
    if result.returncode:
        raise CredentialError("credential_missing_or_locked", "系统凭据不存在、未解锁或保存失败；请解锁凭据库并重新保存密码。")
    if operation == "read":
        if len(result.stdout) > 16384:
            raise CredentialError("invalid_credential", "保存的凭据超过允许长度。")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeError:
            raise CredentialError("invalid_credential", "保存的凭据编码无效，请重新保存密码。") from None


def store_credential(secret: str, reference: str | None = None) -> str:
    """Save or rotate a password; return only an opaque, nonsecret config reference."""
    if not isinstance(secret, str) or "\x00" in secret or len(secret.encode("utf-8")) > 16384:
        raise CredentialError("invalid_credential", "密码必须是长度不超过 16384 字节且不含空字符的文本。")
    backend, identity = _parse(reference) if reference is not None else (_backend(), uuid4().hex)
    (_windows if backend == "windows" else _secret_service)("store", identity, secret)
    return f"one-search-vault:{backend}:{identity}"


def read_credential(reference: str) -> str:
    """Internal connection use only: never expose this result through CLI/MCP/status."""
    backend, identity = _parse(reference)
    return (_windows if backend == "windows" else _secret_service)("read", identity)


def delete_credential(reference: str) -> None:
    backend, identity = _parse(reference)
    (_windows if backend == "windows" else _secret_service)("delete", identity)


def credential_status(reference: str) -> dict:
    try:
        backend, _ = _parse(reference)
        read_credential(reference)
        return {"available": True, "backend": backend, "code": "ready"}
    except CredentialError as error:
        return {"available": False, "code": error.code, "message": str(error)}


def resolve_password(config: dict) -> str | None:
    reference, variable = config.get("credential_ref"), config.get("password_env")
    if reference and variable:
        raise CredentialError("conflicting_credentials", "系统凭据和密码环境变量只能选择一种。")
    if reference:
        return read_credential(reference)
    if variable:
        if not isinstance(variable, str) or variable not in os.environ:
            raise CredentialError("password_environment_missing", "Configured password environment variable is missing")
        return os.environ[variable]
    return None
