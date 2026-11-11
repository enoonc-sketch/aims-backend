"""Reversible encryption for secrets we must send back out (external vendor
site login passwords) — unlike user account passwords (app/core/security.py,
one-way bcrypt hash), the scraper has to hand the *plaintext* password back
to the login form on every sync, so it can't be a one-way hash.

密钥派生（AIMS 1.0 阶段 1，审计项 M-8）：单一 SECRET_KEY 以前同时派生 JWT
签名、refresh HMAC、Fernet 三种用途。这里改为 HKDF 派生 purpose 分离的
Fernet 子密钥（info=b"aims/fernet/v2"），并保留旧 SHA-256 派生作为解密
回退——已有厂商密码无需迁移即可继续解密，新密文一律用 v2。

轮换 SECRET_KEY 仍会使已存厂商密码不可解（重新录入即可）；这是有意的
权衡：不为厂商密码引入独立机密的管理负担。
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

_HKDF_INFO = b"aims/fernet/v2"


def _fernet_v2():
    from cryptography.fernet import Fernet

    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(settings.SECRET_KEY.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def _fernet_legacy():
    from cryptography.fernet import Fernet

    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str) -> str:
    return _fernet_v2().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet_v2().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        # 旧版本（阶段 1 之前）写入的密文用 SHA-256 派生密钥——回退解密，
        # 让升级后的实例仍能登录厂商平台，无需手工迁移数据。
        return _fernet_legacy().decrypt(token.encode("utf-8")).decode("utf-8")
