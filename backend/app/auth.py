"""Authentication and roles.

Opaque random session tokens in an HttpOnly, SameSite=Lax cookie (revocable: deactivating a user or resetting a password
ends their sessions). Passwords are hashed with scrypt. Accounts lock for a while after repeated failed sign-ins.

Roles (each includes the ones before it):
  viewer   - read-only: dashboard, lists, case details, video playback, CSV export
  reviewer - viewer + upload, record review decisions, re-run tests, standalone test APIs
  admin    - reviewer + delete videos, change thresholds and connections, manage users
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response

from .db import T, Db, Row, get_db

ROLES = ("viewer", "reviewer", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
COOKIE = "pivc_session"
SESSION_HOURS = 12
LOCK_AFTER = 5
LOCK_MINUTES = 15
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]{2,39}$")
SCRYPT = (2**14, 8, 1)


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---- passwords -----------------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    n, r, p = SCRYPT
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:  # noqa: BLE001 - malformed hash
        return False


def normalize_username(u: str) -> str:
    u = (u or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise HTTPException(422, "Username must be 3-40 characters: letters, numbers and . _ @ - (starting with a letter or number).")
    return u


def validate_password(password: str, username: str = "") -> None:
    if not isinstance(password, str) or len(password) < 10:
        raise HTTPException(422, "Password must be at least 10 characters.")
    if len(password) > 128:
        raise HTTPException(422, "Password must be at most 128 characters.")
    if username and username.lower() in password.lower():
        raise HTTPException(422, "Password must not contain the username.")
    if len(set(password)) < 4:
        raise HTTPException(422, "Password is too repetitive.")


def public_user(u: Row) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "active": u.active,
            "must_change_password": u.must_change_password, "last_login_at": u.last_login_at, "created_at": u.created_at,
            "locked": bool(u.locked_until and aware(u.locked_until) > now())}


# ---- sessions ------------------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Db, user: Row, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    db.insert("user_sessions", token_hash=_hash_token(token), user_id=user.id, expires_at=now() + timedelta(hours=SESSION_HOURS),
              ip=(request.client.host if request.client else "")[:45], user_agent=(request.headers.get("user-agent") or "")[:200])
    user.last_login_at, user.failed_attempts, user.locked_until = now(), 0, None
    db.save(user)
    db.commit()
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax", secure=secure, path="/")


def end_session(db: Db, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.delete("user_sessions", "token_hash = ?", (_hash_token(token),))
        db.commit()
    response.delete_cookie(COOKIE, path="/")


def revoke_sessions(db: Db, user_id: int, keep_token: str | None = None) -> None:
    if keep_token:
        db.delete("user_sessions", "user_id = ? AND token_hash <> ?", (user_id, _hash_token(keep_token)))
    else:
        db.delete("user_sessions", "user_id = ?", (user_id,))
    db.commit()


def purge_expired(db: Db) -> None:
    db.delete("user_sessions", "expires_at < ?", (now(),))
    db.commit()


def session_user(db: Db, request: Request) -> Row | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    s = db.one(f"SELECT * FROM {T.user_sessions} WHERE token_hash = ?", (_hash_token(token),), "user_sessions")
    if not s or aware(s.expires_at) < now():
        return None
    user = db.get("users", s.user_id)
    if not user or not user.active:
        return None
    if aware(s.expires_at) - now() < timedelta(hours=SESSION_HOURS / 2):  # sliding expiry
        s.expires_at = now() + timedelta(hours=SESSION_HOURS)
        db.save(s)
        db.commit()
    return user


# ---- dependencies --------------------------------------------------------------------------------
def current_user_any(request: Request, db: Db = Depends(get_db)) -> Row:
    """Signed-in user, even if they still have to change their password (used by the few routes that allow that)."""
    user = session_user(db, request)
    if user is None:
        raise HTTPException(401, "Not signed in")
    return user


def current_user(user: Row = Depends(current_user_any)) -> Row:
    if user.must_change_password:
        raise HTTPException(403, "You must change your password before continuing.")
    return user


def require_role(role: str):
    def dep(user: Row = Depends(current_user)) -> Row:
        if RANK[user.role] < RANK[role]:
            raise HTTPException(403, f"This needs the {role} role.")
        return user
    return dep


def other_active_admins(db: Db, exclude_id: int) -> int:
    return db.scalar(f"SELECT COUNT(*) FROM {T.users} WHERE role = 'admin' AND active = 1 AND id <> ?", (exclude_id,))


def user_by_name(db: Db, username: str) -> Row | None:
    return db.one(f"SELECT * FROM {T.users} WHERE username = ?", (username,), "users")


def bootstrap_admin(db: Db) -> None:
    """Create the first administrator from ADMIN_USERNAME / ADMIN_PASSWORD when there are no users yet."""
    from .config import settings
    if db.scalar(f"SELECT COUNT(*) FROM {T.users}") or not (settings.admin_username and settings.admin_password):
        return
    username = normalize_username(settings.admin_username)
    validate_password(settings.admin_password, username)
    db.insert("users", username=username, full_name="Administrator", role="admin", password_hash=hash_password(settings.admin_password))
    db.commit()
