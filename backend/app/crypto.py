"""Encryption of secrets saved from the UI. The key comes from APP_SECRET_KEY (never stored in the DB)."""
import base64
import hashlib
import json

from .config import settings


def _fernet():
    if not settings.app_secret_key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret_key.encode()).digest()))


def can_store_secret() -> bool:
    return _fernet() is not None


def encrypt_dict(d: dict[str, str]) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError("APP_SECRET_KEY is not set")
    return f.encrypt(json.dumps(d).encode()).decode()


def decrypt_dict(token: str | None) -> dict[str, str] | None:
    """None when there is nothing stored or it cannot be decrypted (e.g. APP_SECRET_KEY changed)."""
    f = _fernet()
    if not token or f is None:
        return None
    try:
        return json.loads(f.decrypt(token.encode()).decode())
    except Exception:  # noqa: BLE001
        return None


def decrypt_text(token: str | None) -> str | None:
    """Legacy single-string tokens (pre-connections blob configuration)."""
    f = _fernet()
    if not token or f is None:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001
        return None
