# -*- coding: utf-8 -*-
"""本地凭据解密原语：DPAPI 与 bytecrypto(tc) AES-CBC。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import base64
import ctypes
import hashlib
import json
from typing import Any, Optional


MAGIC = b'tc'


SEED_OFFSET = 6


SEED_LENGTH = 32


CIPHERTEXT_OFFSET = SEED_OFFSET + SEED_LENGTH


CHECKSUM_LENGTH = 64


XOR_MASK = bytes([
    77, 212, 194, 230, 184, 49, 98, 9, 14, 82, 179, 199, 166, 115, 59, 164,
    28, 178, 70, 43, 130, 154, 181, 138, 25, 107, 57, 219, 87, 23, 117, 36,
    244, 155, 175, 127, 8, 232, 214, 141, 38, 167, 46, 55, 193, 169, 90, 47,
    31, 5, 165, 24, 146, 174, 242, 148, 151, 50, 182, 42, 56, 170, 221, 88
])


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def decrypt_bytecrypto(b64_data: str) -> bytes:
    raw = base64.b64decode(b64_data)
    if len(raw) < CIPHERTEXT_OFFSET:
        raise ValueError("密文数据过短")
    if raw[0:2] != MAGIC:
        raise ValueError("无效的密文格式")
    if raw[2] != 5:
        raise ValueError(f"不支持的加密版本: {raw[2]}")
    seed = raw[SEED_OFFSET:CIPHERTEXT_OFFSET]
    h1 = hashlib.sha512(seed).digest()
    h2 = hashlib.sha512(h1 + XOR_MASK).digest()
    aes_key, iv = h2[:16], h2[16:32]
    decrypted = _aes_cbc_decrypt(aes_key, iv, raw[CIPHERTEXT_OFFSET:])
    if len(decrypted) < CHECKSUM_LENGTH:
        raise ValueError("解密数据过短")
    stored_checksum, plaintext = decrypted[:CHECKSUM_LENGTH], decrypted[CHECKSUM_LENGTH:]
    if stored_checksum != hashlib.sha512(plaintext).digest():
        raise ValueError("校验和验证失败——应用可能已更新")
    return plaintext


def decrypt_to_json(b64_data: str) -> Any:
    return json.loads(decrypt_bytecrypto(b64_data).decode("utf-8"))


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_encrypt(data: bytes) -> str:
    """用 Windows DPAPI（当前用户凭据）加密，返回 base64 字符串。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("凭证加密失败（CryptProtectData）")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(b64: str) -> bytes:
    """解密 dpapi_encrypt 产生的 base64 字符串。"""
    raw = base64.b64decode(b64)
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("凭证解密失败（文件可能来自其他电脑或系统账户，请重新保存账号）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
