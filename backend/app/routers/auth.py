from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import activity, auth as A
from ..db import T, Db, Row, get_db

router = APIRouter(prefix="/api/auth")
BAD_LOGIN = "Incorrect username or password."


@router.get("/status")
def status(db: Db = Depends(get_db)):
    """Public: tells the sign-in screen whether the first administrator still has to be created."""
    return {"setup_required": db.scalar(f"SELECT COUNT(*) FROM {T.users}") == 0}


@router.post("/setup")
def setup(body: dict, request: Request, response: Response, db: Db = Depends(get_db)):
    """Create the first administrator. Only works while there are no users at all."""
    if db.scalar(f"SELECT COUNT(*) FROM {T.users}") > 0:
        raise HTTPException(409, "Setup is already complete.")
    username = A.normalize_username(body.get("username", ""))
    A.validate_password(body.get("password", ""), username)
    user = db.insert("users", username=username, full_name=(body.get("full_name") or "Administrator").strip()[:120], role="admin",
                     password_hash=A.hash_password(body["password"]))
    activity.record(db, username, "security", "first administrator created", username, ip=activity.client_ip(request))
    db.commit()
    A.start_session(db, user, request, response)
    return A.public_user(user)


@router.post("/login")
def login(body: dict, request: Request, response: Response, db: Db = Depends(get_db)):
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    user = A.user_by_name(db, username)
    if user is None:
        A.hash_password(password or "x")  # similar work for unknown users, so timing does not reveal them
        activity.record(db, "(not signed in)", "security", "sign-in failed", username[:40], {"reason": "unknown user"}, ip=activity.client_ip(request))
        db.commit()
        raise HTTPException(401, BAD_LOGIN)
    if user.locked_until and A.aware(user.locked_until) > A.now():
        raise HTTPException(423, f"Too many failed attempts. Try again in a few minutes.")
    if not A.verify_password(password, user.password_hash):
        user.failed_attempts += 1
        activity.record(db, "(not signed in)", "security", "sign-in failed", user.username, {"reason": "wrong password"}, ip=activity.client_ip(request))
        if user.failed_attempts >= A.LOCK_AFTER:
            user.locked_until = A.now() + A.timedelta(minutes=A.LOCK_MINUTES)
            user.failed_attempts = 0
            activity.record(db, "system", "security", "account locked", user.username, ip=activity.client_ip(request))
        db.save(user)
        db.commit()
        raise HTTPException(401, BAD_LOGIN)
    if not user.active:
        raise HTTPException(403, "This account is deactivated. Ask an administrator.")
    A.start_session(db, user, request, response)
    activity.record(db, user.username, "security", "signed in", user.username, ip=activity.client_ip(request))
    db.commit()
    return A.public_user(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Db = Depends(get_db)):
    who = A.session_user(db, request)
    if who:
        activity.record(db, who.username, "security", "signed out", who.username, ip=activity.client_ip(request))
        db.commit()
    A.end_session(db, request, response)
    return {"ok": True}


@router.get("/me")
def me(user: Row = Depends(A.current_user_any)):
    return A.public_user(user)


@router.post("/change-password")
def change_password(body: dict, request: Request, user: Row = Depends(A.current_user_any), db: Db = Depends(get_db)):
    if not A.verify_password(body.get("current_password") or "", user.password_hash):
        raise HTTPException(403, "Current password is incorrect.")
    new = body.get("new_password") or ""
    A.validate_password(new, user.username)
    if A.verify_password(new, user.password_hash):
        raise HTTPException(422, "The new password must be different from the current one.")
    user.password_hash, user.must_change_password = A.hash_password(new), False
    activity.record(db, user.username, "security", "password changed", user.username, ip=activity.client_ip(request))
    db.save(user)
    db.commit()
    A.revoke_sessions(db, user.id, keep_token=request.cookies.get(A.COOKIE))  # sign out everywhere else
    return A.public_user(user)
