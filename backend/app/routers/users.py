from fastapi import APIRouter, Depends, HTTPException

from .. import activity, auth as A
from ..db import T, Db, Row, get_db

router = APIRouter(prefix="/api/users", dependencies=[Depends(A.require_role("admin"))])


def _get(db: Db, id_: int) -> Row:
    u = db.get("users", id_)
    if not u:
        raise HTTPException(404, "User not found")
    return u


def _role(r: str) -> str:
    if r not in A.ROLES:
        raise HTTPException(422, f"Role must be one of: {', '.join(A.ROLES)}.")
    return r


@router.get("")
def list_users(db: Db = Depends(get_db)):
    return [A.public_user(u) for u in db.all(f"SELECT * FROM {T.users} ORDER BY username", (), "users")]


@router.post("")
def create_user(body: dict, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    """The new user must change the password at first sign-in."""
    username = A.normalize_username(body.get("username", ""))
    if A.user_by_name(db, username):
        raise HTTPException(409, f"The username '{username}' is already taken.")
    A.validate_password(body.get("password", ""), username)
    u = db.insert("users", username=username, full_name=(body.get("full_name") or "").strip()[:120] or username,
                  role=_role(body.get("role", "viewer")), password_hash=A.hash_password(body["password"]),
                  must_change_password=True, created_by=me.username)
    activity.record(db, me.username, "users", "user created", username, {"role": u.role, "full_name": u.full_name})
    db.commit()
    return A.public_user(u)


@router.put("/{id_}")
def update_user(id_: int, body: dict, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    u = _get(db, id_)
    new_role = _role(body["role"]) if "role" in body else u.role
    new_active = bool(body["active"]) if "active" in body else u.active
    losing_admin = u.role == "admin" and u.active and (new_role != "admin" or not new_active)
    if losing_admin and A.other_active_admins(db, u.id) == 0:
        raise HTTPException(409, "There must always be at least one active administrator.")
    if u.id == me.id and (new_role != u.role or not new_active):
        raise HTTPException(409, "You cannot change your own role or deactivate yourself. Ask another administrator.")
    before = {"role": u.role, "active": u.active, "full_name": u.full_name}
    if "full_name" in body:
        u.full_name = (body["full_name"] or "").strip()[:120] or u.username
    u.role, u.active = new_role, new_active
    changes = {k: {"from": before[k], "to": v} for k, v in {"role": u.role, "active": u.active, "full_name": u.full_name}.items() if before[k] != v}
    if changes:
        activity.record(db, me.username, "users", "user changed", u.username, {"changes": changes})
    db.save(u)
    db.commit()
    if not new_active:
        A.revoke_sessions(db, u.id)
    return A.public_user(u)


@router.post("/{id_}/reset-password")
def reset_password(id_: int, body: dict, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    """Set a temporary password; the user must change it at next sign-in. Ends their sessions and clears any lock."""
    u = _get(db, id_)
    A.validate_password(body.get("password", ""), u.username)
    u.password_hash, u.must_change_password, u.failed_attempts, u.locked_until = A.hash_password(body["password"]), True, 0, None
    activity.record(db, me.username, "users", "password reset", u.username)
    db.save(u)
    db.commit()
    A.revoke_sessions(db, u.id)
    return A.public_user(u)


@router.post("/{id_}/unlock")
def unlock(id_: int, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    u = _get(db, id_)
    u.failed_attempts, u.locked_until = 0, None
    activity.record(db, me.username, "users", "user unlocked", u.username)
    db.save(u)
    db.commit()
    return A.public_user(u)


@router.delete("/{id_}")
def delete_user(id_: int, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    u = _get(db, id_)
    if u.id == me.id:
        raise HTTPException(409, "You cannot delete your own account.")
    if u.role == "admin" and u.active and A.other_active_admins(db, u.id) == 0:
        raise HTTPException(409, "There must always be at least one active administrator.")
    A.revoke_sessions(db, u.id)
    activity.record(db, me.username, "users", "user deleted", u.username, {"role": u.role})
    db.delete("users", "id = ?", (u.id,))
    db.commit()
    return {"deleted": id_}
