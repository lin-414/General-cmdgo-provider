#!/usr/bin/env python3
"""凭据落盘加密 — Windows 上用 DPAPI（当前用户级），其他平台明文回退。

文件格式：
  加密：``CMDGO_DPAPI1:`` 前缀 + base64(DPAPI blob)
  明文：原样 JSON 字节（兼容旧文件；读取时自动识别，下次写入自动升级为加密）

DPAPI 以当前 Windows 用户为密钥边界：token.json / accounts.json 只有同一个
Windows 账号（及同用户进程）能解，复制到别的机器/用户处拿到的只是密文。
"""
from __future__ import annotations

import base64
import os
import random

MAGIC = b"CMDGO_DPAPI1:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _on_windows() -> bool:
    return os.name == "nt"


def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    data_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    data_out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(data_in), None, None, None, None,
                                    _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out)):
        raise OSError("CryptProtectData failed")
    try:
        blob = ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)
    return MAGIC + base64.b64encode(blob)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    raw = base64.b64decode(data[len(MAGIC):].strip())
    buf = ctypes.create_string_buffer(raw, len(raw))
    data_in = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    data_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(data_in), None, None, None, None,
                                      _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out)):
        raise OSError("CryptUnprotectData failed（密文可能来自其他 Windows 用户）")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def protect(data: bytes) -> bytes:
    """加密字节串；非 Windows 平台原样返回（明文回退）。"""
    if not _on_windows():
        return data
    return _dpapi_protect(data)


def unprotect(data: bytes) -> bytes:
    """还原字节串；无 MAGIC 前缀视为旧版明文，原样返回。"""
    if not data.startswith(MAGIC):
        return data
    if not _on_windows():
        raise OSError("文件是 DPAPI 加密的，但当前不是 Windows 平台")
    return _dpapi_unprotect(data)


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def read_credential(path: str) -> bytes:
    """读取凭据文件并解密（兼容旧版明文文件）。"""
    with open(path, "rb") as f:
        return unprotect(f.read())


def write_credential(path: str, data: bytes) -> None:
    """加密（Windows）后原子写入凭据文件（临时文件 + rename）。"""
    payload = protect(data)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{random.randbytes(4).hex()}.tmp"
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 口令加密（跨机器可迁移）：Fernet + PBKDF2-HMAC-SHA256。
# 需要 cryptography 包（GUI 打包版已内置）；惰性导入，缺失时报可操作的错误。
# 文件格式：JSON {"type","version","kdf":{"salt","iterations"},"token"}
# ---------------------------------------------------------------------------
EXPORT_TYPE = "cmdgo-accounts-export"
EXPORT_VERSION = 1
_PBKDF2_ITERATIONS = 600_000


def _fernet(passphrase: str, salt: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError("需要 cryptography 库：pip install cryptography（GUI 打包版已内置）") from e
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import base64 as _b64
    kdf = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERATIONS)
    key = _b64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(key)


def encrypt_with_passphrase(data: bytes, passphrase: str) -> bytes:
    """用口令加密字节串，返回可写入文件的 JSON 信封（跨机器可解）。"""
    if not passphrase:
        raise ValueError("口令不能为空")
    salt = random.randbytes(16)
    token = _fernet(passphrase, salt).encrypt(data)
    import base64 as _b64
    import json as _json
    envelope = {
        "type": EXPORT_TYPE,
        "version": EXPORT_VERSION,
        "kdf": {"salt": _b64.b64encode(salt).decode(), "iterations": _PBKDF2_ITERATIONS},
        "token": token.decode(),
    }
    return _json.dumps(envelope, indent=2).encode("utf-8")


def decrypt_with_passphrase(payload: bytes, passphrase: str) -> bytes:
    """解开 encrypt_with_passphrase 的信封；口令错误或文件损坏抛 ValueError。"""
    import base64 as _b64
    import json as _json
    try:
        envelope = _json.loads(payload)
    except Exception as e:
        raise ValueError("文件不是有效的导出信封") from e
    if not isinstance(envelope, dict) or envelope.get("type") != EXPORT_TYPE:
        raise ValueError("文件不是 cmdgo 账号导出文件")
    try:
        salt = _b64.b64decode(envelope["kdf"]["salt"])
        iterations = int(envelope["kdf"].get("iterations", _PBKDF2_ITERATIONS))
        token = envelope["token"].encode()
    except Exception as e:
        raise ValueError("导出信封字段缺失或损坏") from e
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=iterations)
    key = _b64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    try:
        return Fernet(key).decrypt(token)
    except InvalidToken as e:
        raise ValueError("口令错误或文件已损坏") from e
