"""The history of test runs: every analysis that is requested (the first one after an upload, a re-run of everything, or a re-run
of just some tests) is recorded when it is queued and updated as it moves through running to done or failed.

It deliberately says nothing about which engine or model did the work; it speaks in test names a reviewer knows.
"""
import json
from datetime import datetime, timezone

from .db import T, Db, Row

OPEN = ("QUEUED", "RUNNING")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create(db: Db, case: Row, tests: list[str] | None, by: str) -> Row:
    """Record a requested run (call it in the same transaction that queues the video)."""
    kind = "tests" if tests else ("all" if case.report else "first")
    return db.insert("runs", case_id=case.id, case_ref=case.case_ref, filename=(case.media or {}).get("original_name") or case.case_ref,
                     kind=kind, tests=tests, requested_by=by)


def start(db: Db, run_id: int | None) -> None:
    if run_id:
        db.run(f"UPDATE {T.runs} SET status = 'RUNNING', started_at = ? WHERE id = ? AND status = 'QUEUED'", (_now(), run_id))
        db.commit()


def finish(db: Db, run_id: int | None, status: str, before: dict | None = None, after: dict | None = None,
           decision_before: str | None = None, decision_after: str | None = None, error: str | None = None) -> None:
    if run_id:
        db.run(f"UPDATE {T.runs} SET status = ?, finished_at = ?, [before] = ?, [after] = ?, decision_before = ?, decision_after = ?, error = ? WHERE id = ?",
               (status, _now(), json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None, decision_before, decision_after, (error or "")[:300] or None, run_id))
        db.commit()


def cancel_open(db: Db, case_id: int, reason: str) -> None:
    db.run(f"UPDATE {T.runs} SET status = 'CANCELLED', finished_at = ?, error = ? WHERE case_id = ? AND status IN ('QUEUED', 'RUNNING')",
           (_now(), reason, case_id))
