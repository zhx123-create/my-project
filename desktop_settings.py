"""Small desktop preferences and explicit current-user Windows DPAPI storage."""
from __future__ import annotations

import base64
import binascii
import copy
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
from server import normalize_api_base

_SCHEMA = "jiehua.desktop-settings"
_MAX_BYTES = 16 * 1024
_DEFAULTS = {"base": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "allow_http": False,
             "auto_copy": False, "watch_interval": 10, "last_profile": "", "capture_source": "窗口文字", "max_output_tokens": 4096}


def supports_secure_storage() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Use the logged-in Windows account, never machine-wide protection."""
    if not supports_secure_storage():
        raise ValueError("记住密钥仅支持 Windows 当前用户加密存储。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    try:
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel.LocalFree.restype = ctypes.c_void_p
        source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        if protect:
            operation = crypt.CryptProtectData
            operation.argtypes = (ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob))
            arguments = (ctypes.byref(source), "JieHua API credential", None, None, None, 1, ctypes.byref(target))
        else:
            operation = crypt.CryptUnprotectData
            operation.argtypes = (ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob))
            arguments = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
        operation.restype = wintypes.BOOL
        try:
            if not operation(*arguments) or not target.pbData or target.cbData > _MAX_BYTES:
                raise ValueError("Windows 无法处理已保存的密钥，请重新填写或关闭记住密钥。")
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            if target.pbData:
                kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))
    except (AttributeError, OSError):
        raise ValueError("Windows 密钥加密服务不可用，请仅在本次运行使用密钥。") from None


def _plain(value, limit, label, empty=False):
    if not isinstance(value, str) or len(value) > limit or any(not char.isprintable() for char in value):
        raise ValueError(f"{label}格式不正确或过长。")
    value = value.strip()
    if not empty and not value:
        raise ValueError(f"请填写{label}。")
    return value


def _values(raw):
    if not isinstance(raw, dict) or set(raw) - set(_DEFAULTS):
        raise ValueError("桌面设置包含不支持的字段。")
    result = {**_DEFAULTS, **raw}
    result["base"] = normalize_api_base(result["base"], result["allow_http"])
    result["model"] = _plain(result["model"], 100, "模型名称")
    result["last_profile"] = _plain(result["last_profile"], 100, "会话名称", empty=True)
    if result["capture_source"] not in ("窗口文字", "截图转文字"):
        raise ValueError("采集方式请选择窗口文字或截图转文字。")
    if type(result["auto_copy"]) is not bool:
        raise ValueError("自动复制开关必须为布尔值。")
    if type(result["watch_interval"]) is not int or result["watch_interval"] not in (5, 10, 20, 30):
        raise ValueError("检查间隔请选择 5、10、20 或 30 秒。")
    if type(result["max_output_tokens"]) is not int or not 512 <= result["max_output_tokens"] <= 16384:
        raise ValueError("输出上限应为 512 至 16384 的整数。")
    return result


class DesktopSettings:
    """Non-secret settings save normally; credentials save only when requested.

    Constructing with ``path=None`` never reads or creates any file. api_key()
    returns an empty string on other operating systems; a failed Windows
    decryption raises a fixed ValueError so the UI can request fresh input.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._values = dict(_DEFAULTS)
        self._encrypted_key = ""
        self._check_path()
        if self.path is not None and self.path.exists():
            self._load()

    def _check_path(self):
        if self.path is not None and self.path.is_symlink():
            raise ValueError("设置文件是符号链接，未读取或修改。")

    def values(self) -> dict:
        return {**copy.deepcopy(self._values), "remember_key": bool(self._encrypted_key)}

    def api_key(self) -> str:
        if not self._encrypted_key or not supports_secure_storage():
            return ""
        try:
            raw = base64.b64decode(self._encrypted_key, validate=True)
            return _plain(_dpapi(raw, protect=False).decode("utf-8"), 4096, "已保存密钥")
        except (ValueError, UnicodeError, binascii.Error):
            raise ValueError("已保存的密钥无法解密，请重新填写或关闭记住密钥。") from None

    def update(self, **fields):
        if isinstance(fields.get("base"), str) and fields["base"].strip().rstrip("/") != self._values["base"]:
            fields.setdefault("allow_http", False)
        values = _values({**self._values, **fields})
        self._save(values, self._encrypted_key)
        self._values = values

    def set_api(self, base: str, model: str, key: str, remember: bool, allow_http: bool = False, max_output_tokens=None):
        if type(remember) is not bool:
            raise ValueError("记住密钥开关必须为布尔值。")
        fields = {"base": base, "model": model, "allow_http": allow_http}
        if max_output_tokens is not None:
            fields["max_output_tokens"] = max_output_tokens
        values = _values({**self._values, **fields})
        key = _plain(key, 4096, "API 密钥", empty=not remember)
        encrypted = ""
        if remember:
            if not supports_secure_storage():
                raise ValueError("记住密钥仅支持 Windows 当前用户加密存储。")
            encrypted = base64.b64encode(_dpapi(key.encode("utf-8"), protect=True)).decode("ascii")
            if not encrypted:
                raise ValueError("Windows 未能加密密钥，请仅在本次运行使用。")
        self._save(values, encrypted)
        self._values, self._encrypted_key = values, encrypted

    def _load(self):
        try:
            if not self.path.is_file() or self.path.stat().st_size > _MAX_BYTES:
                raise ValueError("invalid size")
            with self.path.open("rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError("invalid size")
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) - {"schema", "version", "values", "encrypted_key"} or data.get("schema") != _SCHEMA or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("invalid schema")
            values = _values(data.get("values"))
            encrypted = data.get("encrypted_key", "")
            if not isinstance(encrypted, str) or len(encrypted) > 12000:
                raise ValueError("invalid credential")
            if encrypted:
                base64.b64decode(encrypted, validate=True)
            self._values, self._encrypted_key = values, encrypted
        except (OSError, ValueError, TypeError, UnicodeError, binascii.Error):
            raise ValueError("桌面设置文件损坏或格式不支持，原文件未改动。") from None

    def _save(self, values, encrypted):
        if self.path is None:
            return
        self._check_path()
        temporary = None
        try:
            data = json.dumps({"schema": _SCHEMA, "version": 1, "values": values,
                               "encrypted_key": encrypted}, ensure_ascii=False).encode("utf-8")
            if len(data) > _MAX_BYTES:
                raise ValueError("桌面设置内容过长，未保存。")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent, prefix=".jiehua-settings-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_path()
            os.replace(temporary, self.path)
        except OSError:
            raise ValueError("桌面设置无法保存，请检查本地文件位置和权限。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
