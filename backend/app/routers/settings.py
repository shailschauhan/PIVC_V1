from fastapi import APIRouter, Depends, HTTPException

from .. import activity, auth as A
from .. import connections, crypto, storage
from ..db import Db, Row, get_db

router = APIRouter(prefix="/api/settings", dependencies=[Depends(A.require_role("admin"))])


def _row(db: Db, id_: int) -> Row:
    row = connections.get(db, id_)
    if not row:
        raise HTTPException(404, "Connection not found")
    return row


def _bad(e: Exception) -> HTTPException:
    return HTTPException(422, str(e))


@router.get("/connection-types")
def connection_types():
    return {
        "types": [{"type": k, **v} for k, v in connections.TYPES.items()],
        "can_save_secret": crypto.can_store_secret(),
    }


@router.get("/connections")
def list_connections(type: str | None = None, db: Db = Depends(get_db)):
    return [connections.public(r) for r in connections.list_all(db, type)]


@router.post("/connections")
def create_connection(body: dict, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    try:
        row = connections.create(db, body.get("type", ""), body.get("name", ""), body.get("fields") or {}, body.get("secrets") or {})
    except connections.ConnectionInputError as e:
        raise _bad(e) from e
    activity.record(db, me.username, "settings", "connection created", row.name, {"type": row.type, "connection_id": row.id})
    db.commit()
    storage._cache.clear()
    return connections.public(row)


@router.put("/connections/{id_}")
def update_connection(id_: int, body: dict, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    """Secrets omitted or blank are kept as saved; list a key in `clear_secrets` to remove it."""
    row = _row(db, id_)
    before = {"name": row.name, **row.fields}
    try:
        connections.update(db, row, body.get("name"), body.get("fields") or {}, body.get("secrets") or {}, body.get("clear_secrets") or [])
    except connections.ConnectionInputError as e:
        raise _bad(e) from e
    # names of what changed, never values: secrets must not reach the log
    changed = [k for k, v in {"name": row.name, **row.fields}.items() if before.get(k) != v]
    changed += [f"{k} (secret)" for k, v in (body.get("secrets") or {}).items() if isinstance(v, str) and v.strip()]
    changed += [f"{k} (secret removed)" for k in body.get("clear_secrets") or []]
    activity.record(db, me.username, "settings", "connection updated", row.name, {"type": row.type, "connection_id": row.id, "changed": changed})
    db.commit()
    storage._cache.clear()
    return connections.public(row)


@router.post("/connections/{id_}/activate")
def activate_connection(id_: int, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    row = _row(db, id_)
    connections.activate(db, row)
    activity.record(db, me.username, "settings", "connection activated", row.name, {"type": row.type, "connection_id": row.id})
    db.commit()
    storage._cache.clear()
    return connections.public(row)


@router.post("/connections/{id_}/deactivate")
def deactivate_connection(id_: int, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    """For blob storage this switches new uploads back to local disk."""
    row = _row(db, id_)
    row.active = False
    db.save(row)
    activity.record(db, me.username, "settings", "connection deactivated", row.name, {"type": row.type, "connection_id": row.id})
    db.commit()
    storage._cache.clear()
    return connections.public(row)


@router.delete("/connections/{id_}")
def delete_connection(id_: int, me: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    row = _row(db, id_)
    if row.active:
        raise HTTPException(409, "This connection is active. Deactivate it (or activate another) before deleting.")
    activity.record(db, me.username, "settings", "connection deleted", row.name, {"type": row.type, "connection_id": row.id})
    db.delete("connections", "id = ?", (row.id,))
    db.commit()
    return {"deleted": id_}


@router.post("/connections/test")
def test_connection(body: dict, db: Db = Depends(get_db)):
    """Try the submitted values (merged over the saved connection when `id` is given). Nothing is saved."""
    type_, fields, secrets = body.get("type", ""), dict(body.get("fields") or {}), dict(body.get("secrets") or {})
    if body.get("id"):
        row = _row(db, int(body["id"]))
        type_, fields = row.type, {**row.fields, **fields}
        secrets = {**connections.secrets_of(row), **{k: v for k, v in secrets.items() if isinstance(v, str) and v.strip()}}
    try:
        return connections.test(type_, fields, secrets)
    except connections.ConnectionInputError as e:
        return {"ok": False, "message": str(e)}
