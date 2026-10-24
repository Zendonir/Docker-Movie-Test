"""Optionale Benutzeranmeldung (ein lokaler Benutzer, signierte Sitzungs-Cookies)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE_NAME = "mis_session"
SESSION_TTL = 7 * 24 * 3600

_secret: bytes | None = None


def init_secret(config_dir: Path) -> None:
    global _secret
    path = config_dir / "secret.key"
    if path.exists():
        _secret = path.read_bytes().strip()
    else:
        _secret = secrets.token_hex(32).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(_secret)


def _key() -> bytes:
    if _secret is None:
        raise RuntimeError("Sitzungsschlüssel nicht initialisiert")
    return _secret


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Das Passwort muss mindestens 8 Zeichen lang sein")
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def make_session(username: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = f"{username}|{exp}".encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode() + "." + sig


def check_session(token: str | None, username: str | None) -> bool:
    if not token or not username or "." not in token:
        return False
    b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(b64.encode())
    except (ValueError, TypeError):
        return False
    expected = hmac.new(_key(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        user, exp = payload.decode().rsplit("|", 1)
        return user == username and int(exp) > time.time()
    except ValueError:
        return False
