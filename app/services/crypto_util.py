"""Reversible encryption for secrets we must send back out (external vendor
site login passwords) — unlike user account passwords (app/core/security.py,
one-way bcrypt hash), the scraper has to hand the *plaintext* password back
to the login form on every sync, so it can't be a one-way hash.

Key is derived from settings.SECRET_KEY via SHA-256, so no separate secret
needs to be provisioned — but that also means rotating SECRET_KEY makes any
previously-stored external-site passwords undecryptable; re-enter them after
a key rotation.
"""
from __future__ import annotations

import base64
import hashlib

from app.core.config import settings


def _fernet():
    from cryptography.fernet import Fernet

    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
