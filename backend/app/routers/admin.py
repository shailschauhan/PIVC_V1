"""Audit log, rules and versions, engine health, and report export."""
import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from .. import activity, engines, interpret, llm, parameters, replay, reportgen, rules as R
from .. import auth as A
from ..config import DEFAULT_RULES
from ..db import T, Db, Row, get_db, like, marks, open_db

REVIEWER = Depends(A.require_role("reviewer"))
ADMIN = Depends(A.require_role("admin"))
router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- audit log (reviewers and administrators)
def _audit_where(q, category, action, actor, date_from, date_to) -> tuple[str, list]:
    sql, args = "1 = 1", []
    for col, vals in (("category", category), ("action", action), ("actor", actor)):
        if vals:
            sql, args = sql + f" AND {col} IN ({marks(len(vals))})", args + list(vals)
    try:
        if date_from:
            sql, args = sql + " AND at >= ?", args + [reportgen._when(date_from, "from")]
        if date_to:
            sql, args = sql + " AND at <= ?", args + [reportgen._when(date_to, "to")]
    except reportgen.FilterError as e:
        raise HTTPException(422, str(e)) from e
    if q and q.strip():
        pat = like(q.strip())
        sql += " AND (actor LIKE ? ESCAPE '!' OR action LIKE ? ESCAPE '!' OR target LIKE ? ESCAPE '!' OR detail LIKE ? ESCAPE '!')"
        args += [pat] * 4
    return sql, args


def _audit_row(a: Row) -> dict:
    return {"id": a.id, "at": a.at, "actor": a.actor, "category": a.category, "action": a.action, "target": a.target,
            "case_id": a.case_id, "summary": activity.describe(a), "detail": a.detail, "ip": a.ip}


@router.get("/audit", dependencies=[REVIEWER])
def audit(q: str = "", category: list[str] = Query([]), action: list[str] = Query([]), actor: list[str] = Query([]),
          date_from: str | None = None, date_to: str | None = None,
          page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), db: Db = Depends(get_db)):
    sql, args = _audit_where(q, category, action, actor, date_from, date_to)
    total = db.scalar(f"SELECT COUNT(*) FROM {T.activity_log} WHERE {sql}", args)
    rows = db.all(f"SELECT * FROM {T.activity_log} WHERE {sql} ORDER BY id DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
                  [*args, (page - 1) * page_size, page_size], "activity_log")
    return {"total": total, "page": page, "page_size": page_size, "items": [_audit_row(a) for a in rows]}


@router.get("/audit/facets", dependencies=[REVIEWER])
def audit_facets(db: Db = Depends(get_db)):
    return {"categories": [{"key": k, "label": v} for k, v in activity.LABELS.items()],
            "actions": [r.action for r in db.all(f"SELECT DISTINCT action FROM {T.activity_log} ORDER BY action")],
            "actors": [r.actor for r in db.all(f"SELECT DISTINCT TOP 500 actor FROM {T.activity_log} ORDER BY actor")]}


@router.get("/audit/export.csv")
def audit_export(q: str = "", category: list[str] = Query([]), action: list[str] = Query([]), actor: list[str] = Query([]),
                 date_from: str | None = None, date_to: str | None = None, me: Row = Depends(A.require_role("reviewer")),
                 db: Db = Depends(get_db)):
    sql, args = _audit_where(q, category, action, actor, date_from, date_to)
    n = db.scalar(f"SELECT COUNT(*) FROM {T.activity_log} WHERE {sql}", args)
    activity.record(db, me.username, "reports", "audit exported", "", {"rows": n, "filters": {"q": q, "category": category, "action": action,
                                                                                         "actor": actor, "from": date_from, "to": date_to}})
    db.commit()

    def gen():
        with open_db() as own:  # the request's connection may already be released while the file is still being sent
            yield from lines(own)

    def lines(own: Db):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Time (UTC)", "Who", "Area", "Action", "What", "Summary", "Details", "IP address"])
        yield "﻿" + buf.getvalue()
        buf.seek(0), buf.truncate()
        for i, a in enumerate(own.stream(f"SELECT * FROM {T.activity_log} WHERE {sql} ORDER BY id DESC", args, "activity_log", 500), 1):
            w.writerow([reportgen._cell(v) for v in (a.at, a.actor, activity.LABELS.get(a.category, a.category), a.action, a.target,
                                                     activity.describe(a), str(a.detail), a.ip)])
            if i % 500 == 0:
                yield buf.getvalue()
                buf.seek(0), buf.truncate()
        yield buf.getvalue()

    return StreamingResponse(gen(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=pivc_audit_log.csv"})


# ---------------------------------------------------------------- rules and versions (administrators)
def _rules_payload(db: Db) -> dict:
    hist = R.history(db, 1)
    cur = hist[0] if hist else None
    return {"rules": R.load_rules(db), "defaults": DEFAULT_RULES, "version": cur["version"] if cur else 0,
            "updated_at": cur["at"] if cur else None, "updated_by": cur["by"] if cur else None, "note": cur["note"] if cur else None}


class RulesIn(BaseModel):
    changes: dict
    note: str = ""


@router.get("/rules", dependencies=[ADMIN])
def get_rules(db: Db = Depends(get_db)):
    return _rules_payload(db)


@router.put("/rules")
def put_rules(body: RulesIn, me: Row = ADMIN, db: Db = Depends(get_db)):
    return save_with_audit(db, me, body.changes, body.note)


def save_with_audit(db: Db, me: Row, changes: dict, note: str = "", restored_from: int | None = None) -> dict:
    current = R.load_rules(db)
    try:
        merged = R.validate(changes, current)
    except R.RuleError as e:
        raise HTTPException(422, str(e)) from e
    changed = R.diff(current, merged)
    if not changed:
        raise HTTPException(409, "Nothing to save: these values are already the current rules.")
    R.save_rules(db, merged, me.username, note)
    version = R.history(db, 1)[0]["version"]
    activity.record(db, me.username, "rules", "rules restored" if restored_from else "rules changed", str(version),
                    {"changes": changed, "note": note.strip()[:300], **({"restored_from": restored_from} if restored_from else {})})
    db.commit()
    return _rules_payload(db)


@router.get("/rules/history", dependencies=[ADMIN])
def rules_history(db: Db = Depends(get_db)):
    return {"versions": R.history(db)}


@router.post("/rules/preview", dependencies=[ADMIN])
def rules_preview(body: dict, days: int = Query(30, ge=0, le=365), db: Db = Depends(get_db)):
    """What would change if these values were saved? Nothing is saved. `body` is {key: new value}."""
    current = R.load_rules(db)
    try:
        proposed = R.validate(body, current)
    except R.RuleError as e:
        raise HTTPException(422, str(e)) from e
    return replay.impact(db, current, proposed, days)


@router.post("/rules/{version}/restore")
def restore_rules(version: int, me: Row = ADMIN, db: Db = Depends(get_db)):
    old = R.version_rules(db, version)
    if old is None:
        raise HTTPException(404, "No such version")
    return save_with_audit(db, me, old, f"Restored version {version}", restored_from=version)


# ---------------------------------------------------------------- engine and service health (administrators)
@router.get("/engines", dependencies=[ADMIN])
def engines_overview(db: Db = Depends(get_db)):
    return {"checked_at": datetime.now(timezone.utc), "engines": engines.overview(db)}


@router.post("/engines/check", dependencies=[ADMIN])
def engines_check(db: Db = Depends(get_db)):
    """Probe the services now instead of using the cached answer."""
    return {"checked_at": datetime.now(timezone.utc), "engines": engines.overview(db, force=True)}


@router.get("/engines/{key}", dependencies=[ADMIN])
def engine_detail(key: str, hours: int = Query(24, ge=1, le=720), db: Db = Depends(get_db)):
    if key not in parameters.BY_KEY:
        raise HTTPException(404, "No such engine")
    item = next(e for e in engines.overview(db, force=True) if e["key"] == key)
    buckets = 24 if hours <= 48 else min(hours // 24, 30)
    return {**item, "stats": engines.stats(db, key, hours, buckets)}


# ---------------------------------------------------------------- reports (reviewers and administrators)
def _filters(date_from, date_to, decision, review_status, test, q) -> reportgen.Filters:
    try:
        return reportgen.parse(date_from, date_to, decision, review_status, test, q)
    except reportgen.FilterError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/reports/options", dependencies=[REVIEWER])
def report_options():
    return reportgen.catalogue()


@router.get("/reports/preview", dependencies=[REVIEWER])
def report_preview(date_from: str | None = None, date_to: str | None = None, decision: list[str] = Query([]),
                   review_status: list[str] = Query([]), test: list[str] = Query([]), q: str = "", db: Db = Depends(get_db)):
    f = _filters(date_from, date_to, decision, review_status, test, q)
    return {**reportgen.summary(db, f), "filters": reportgen.describe(f)}


@router.get("/reports/export")
def report_export(format: str = Query("csv", pattern="^(csv|pdf)$"), date_from: str | None = None, date_to: str | None = None,
                  decision: list[str] = Query([]), review_status: list[str] = Query([]), test: list[str] = Query([]), q: str = "",
                  columns: list[str] = Query([]), ai: bool = False, me: Row = Depends(A.require_role("reviewer")), db: Db = Depends(get_db)):
    f = _filters(date_from, date_to, decision, review_status, test, q)
    total = reportgen.count(db, f)
    overview = None
    if ai and format == "pdf":  # an AI-written overview of the counts; asked for before anything is recorded or sent
        try:
            overview = interpret.overview(db, reportgen.summary(db, f), reportgen.describe(f))
        except llm.NotConfigured as e:
            raise HTTPException(503, str(e)) from e
        except llm.LlmError as e:
            raise HTTPException(502, str(e)) from e
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    activity.record(db, me.username, "reports", "report exported", "", {"format": format, "rows": total, "filters": reportgen.describe(f), "ai_overview": overview is not None})
    db.commit()
    if format == "pdf":
        keys = reportgen.pick_columns(columns, reportgen.PDF_MAX_COLUMNS)
        pdf = reportgen.build_pdf(db, f, keys, me.full_name or me.username, overview)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=pivc_report_{stamp}.pdf"})
    keys = reportgen.pick_columns(columns)
    def gen():
        with open_db() as own:
            yield from reportgen.stream_csv(own, f, keys)

    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=pivc_report_{stamp}.csv"})
