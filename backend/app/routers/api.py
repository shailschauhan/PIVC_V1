import csv
import io
import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from typing import Literal

from pydantic import BaseModel, Field

from ..config import DEFAULT_RULES, settings
from .. import jobs, storage
from ..dashboard import build as build_dashboard
from ..analyzers.base import Context
from ..analyzers.checks import age_estimation as age_rule, vitals as vitals_rule
from ..authenticity.detector import engine_status
from ..vitals.analyzer import VitalsUnavailable, engine_status as vitals_status, get_analyzer
from ..age.analyzer import AgeUnavailable, engine_status as age_status, get_analyzer as get_age_analyzer
from .. import parameters
from ..db import T, Db, Row, get_db, like, marks, open_db
from ..media import ensure_preview
from .. import activity, casepdf, interpret, llm, reportgen, runs
from fastapi.responses import Response
from .admin import save_with_audit
from ..rules import load_rules, save_rules
from .. import auth as A

router = APIRouter(prefix="/api", dependencies=[Depends(A.current_user)])  # every route needs a signed-in user
public = APIRouter(prefix="/api")  # health only
REVIEWER = [Depends(A.require_role("reviewer"))]
ADMIN = [Depends(A.require_role("admin"))]

DECISIONS = {"Accept", "Reject", "Needs Review"}


def _summary(c: Row) -> dict:
    params = (c.report or {}).get("parameters", {})
    d, h, a = params.get("video_authenticity"), params.get("health_indicators"), params.get("age_estimation")
    fm, lv = params.get("face_match"), params.get("liveness")
    return {
        "id": c.id, "case_ref": c.case_ref, "filename": (c.media or {}).get("original_name") or c.case_ref,
        "reference": c.proposal_no if c.proposal_no != "-" else None,
        "status": c.status, "decision": c.decision, "confidence": c.confidence,
        "authenticity": d["status"] if d else None,
        "fake_score": d["details"].get("fake_score") if d else None,
        "vitals": h["status"] if h else None, "heart_rate": h["details"].get("hr_bpm") if h else None,
        "face_status": fm["status"] if fm else None, "similarity": fm["details"].get("similarity") if fm else None,
        "liveness_status": lv["status"] if lv else None, "real_probability": lv["details"].get("real_probability") if lv else None,
        "has_id_document": bool((c.media or {}).get("id_document")),
        "age_status": a["status"] if a else None, "estimated_age": a["details"].get("age") if a else None,
        "declared_age": a["details"].get("declared_age") if a else None,
        "review_status": c.review_status, "final_decision": c.final_decision,
        "exception_count": len(c.report["exception_details"]) if c.report else 0,
        "created_at": c.created_at, "analyzed_at": c.analyzed_at, "processing_ms": c.processing_ms,
    }


def _detail(c: Row, db: Db) -> dict:
    audit = db.all(f"SELECT * FROM {T.audit_log} WHERE case_id = ? ORDER BY id", (c.id,), "audit_log")
    return {
        **_summary(c), "report": c.report, "error": c.error,
        "reviewer": c.reviewer, "review_notes": c.review_notes, "reviewed_at": c.reviewed_at,
        "interpretation": interpret.view(c) if c.report else None,
        "audit": [{"actor": a.actor, "action": a.action, "detail": a.detail, "at": a.at} for a in audit],
    }


def _get(db: Db, case_id: int) -> Row:
    c = db.get("cases", case_id)
    if not c:
        raise HTTPException(404, "Case not found")
    return c


@public.get("/health")
def health():
    """Liveness + model readiness. `ready` is false until the DFSpot checkpoints are installed."""
    st = engine_status()
    return {"status": "ok" if st["ready"] else "degraded", "authenticity": st, "vitals": vitals_status(), "age": age_status()}


@router.get("/cases")
def list_cases(
    status: str | None = None, decision: str | None = None, review_status: str | None = None,
    q: str | None = None, parameter: str | None = None, parameter_status: str | None = None,
    days: int = Query(0, ge=0), sort: str = "newest",
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
    db: Db = Depends(get_db),
):
    sql, args = "1 = 1", []
    if parameter and parameter_status:
        sql += f" AND id IN (SELECT case_id FROM {T.case_params} WHERE parameter = ? AND status = ?)"
        args += [parameter, parameter_status]
    if days:
        sql, args = sql + " AND created_at >= ?", args + [datetime.now(timezone.utc) - timedelta(days=days)]
    for col, val in (("status", status), ("decision", decision), ("review_status", review_status)):
        if val:
            sql, args = sql + f" AND {col} = ?", args + [val]
    if q:
        sql += " AND (case_ref LIKE ? ESCAPE '!' OR customer_name LIKE ? ESCAPE '!' OR proposal_no LIKE ? ESCAPE '!')"
        args += [like(q)] * 3
    total = db.scalar(f"SELECT COUNT(*) FROM {T.cases} WHERE {sql}", args)
    order = "analyzed_at ASC, id ASC" if sort == "oldest" else "id DESC"
    rows = db.all(f"SELECT * FROM {T.cases} WHERE {sql} ORDER BY {order} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
                  [*args, (page - 1) * page_size, page_size], "cases")
    return {"total": total, "page": page, "page_size": page_size, "items": [_summary(c) for c in rows]}


@router.get("/cases/{case_id}")
def get_case(case_id: int, db: Db = Depends(get_db)):
    return _detail(_get(db, case_id), db)


def _declared(declared_age: str, dob: str) -> dict:
    """Optional details supplied with an upload, validated. Used to verify the estimated age."""
    out: dict = {}
    if declared_age.strip():
        try:
            age = int(float(declared_age))
        except ValueError:
            raise HTTPException(422, "declared_age must be a number") from None
        if not 0 <= age <= 120:
            raise HTTPException(422, "declared_age must be between 0 and 120")
        out["declared_age"] = age
    if dob.strip():
        try:
            d = datetime.fromisoformat(dob.strip()).date()
        except ValueError:
            raise HTTPException(422, "dob must be YYYY-MM-DD") from None
        if d > datetime.now().date():
            raise HTTPException(422, "dob cannot be in the future")
        out["dob"] = d.isoformat()
    return out


def _save_video(dest: Path, f: UploadFile) -> None:
    """Stream to disk, enforcing the size cap."""
    limit, written = settings.max_upload_mb * 1024 * 1024, 0
    with dest.open("wb") as out:
        while chunk := f.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"{f.filename}: larger than {settings.max_upload_mb} MB")
            out.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"{f.filename}: empty file")


@router.post("/cases/upload", dependencies=REVIEWER)
def upload_videos(
    files: list[UploadFile] = File(...), reference: str = Form(""), declared_age: str = Form(""), dob: str = Form(""),
    id_document: UploadFile | None = File(None),
    user: Row = Depends(A.current_user), db: Db = Depends(get_db),
):
    """Upload one or more PIVC videos. Each video becomes a case and is queued for authenticity analysis."""
    st = engine_status()
    if not st["ready"]:
        raise HTTPException(503, f"Analysis models are not available: {st['reason']}")
    bad = [f.filename for f in files if Path(f.filename or "").suffix.lower() not in settings.allowed_video_extensions]
    if bad:
        raise HTTPException(422, f"Unsupported file type: {', '.join(bad)}. Allowed: {', '.join(settings.allowed_video_extensions)}")
    declared = _declared(declared_age, dob)
    id_bytes, id_ext = None, ""
    if id_document is not None and id_document.filename:
        id_ext = Path(id_document.filename).suffix.lower()
        if id_ext not in settings.allowed_image_extensions:
            raise HTTPException(422, f"ID document must be an image ({', '.join(settings.allowed_image_extensions)}).")
        id_bytes = id_document.file.read(settings.max_id_document_mb * 1024 * 1024 + 1)
        if not id_bytes:
            raise HTTPException(422, "The ID document file is empty.")
        if len(id_bytes) > settings.max_id_document_mb * 1024 * 1024:
            raise HTTPException(413, f"The ID document is larger than {settings.max_id_document_mb} MB.")
    created = []
    for f in files:
        ref = f"VID-{uuid.uuid4().hex[:10].upper()}"
        folder = Path(settings.storage_dir) / ref
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"video{Path(f.filename).suffix.lower()}"
        try:
            _save_video(dest, f)
        except HTTPException:
            shutil.rmtree(folder, ignore_errors=True)
            for c in created:  # all-or-nothing per request: nothing was committed yet
                shutil.rmtree(Path(settings.storage_dir) / c.case_ref, ignore_errors=True)
            db.rollback()
            raise
        if id_bytes is not None:
            (folder / f"id_document{id_ext}").write_bytes(id_bytes)
        name = Path(f.filename).name
        c = db.insert("cases", case_ref=ref, proposal_no=reference or "-", customer_id="-", customer_name=name,
                      source="upload", declared=declared,
                      media={"video": str(dest), "original_name": name, **({"id_document": str(folder / f"id_document{id_ext}")} if id_bytes is not None else {})})
        activity.case_event(db, c, user.username, "uploaded", {"filename": name})
        created.append(c)
    run_ids = {c.id: runs.create(db, c, None, user.username).id for c in created}
    db.commit()
    for c in created:
        jobs.enqueue(c.id, None, run_ids[c.id])
    return {"created": [_summary(c) for c in created]}


@router.get("/cases/{case_id}/id-document", dependencies=REVIEWER)
def case_id_document(case_id: int, db: Db = Depends(get_db)):
    """The ID document photo supplied with the upload, for side-by-side review. Reviewers and administrators only."""
    c = _get(db, case_id)
    uri = (c.media or {}).get("id_document")
    if not uri:
        raise HTTPException(404, "No ID document was provided for this video")
    try:
        path = storage.materialize(uri, db)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Could not load the ID document: {e}") from e
    if not Path(path).is_file():
        raise HTTPException(404, "ID document file not found")
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


@router.api_route("/cases/{case_id}/video", methods=["GET", "HEAD"])
def case_video(case_id: int, db: Db = Depends(get_db)):
    """Serve a browser-playable copy of the video (range requests supported), from local disk or blob storage."""
    c = _get(db, case_id)
    media = c.media or {}
    uri = media.get("preview") or media.get("video") or ""
    try:
        if storage.is_blob(uri):
            path = storage.materialize(uri, db)  # cached download
            if uri == media.get("video"):  # no preview stored yet: make one next to the cached original
                path = ensure_preview(str(path))
        else:
            if not Path(uri).is_file():
                raise HTTPException(404, "Video file not found")
            path = ensure_preview(uri)  # H.264 copy when the original codec is not browser-playable
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Could not prepare a playable video: {e}") from e
    return FileResponse(path)


@router.post("/vitals/analyze", dependencies=REVIEWER)
def analyze_vitals(file: UploadFile = File(...), db: Db = Depends(get_db)):
    """Measure vital signs (heart rate, HRV, breathing) from one face video and return the result immediately.
    Nothing is stored. Takes a few seconds to a minute; for case videos the same test runs automatically."""
    st = vitals_status()
    if not st["ready"]:
        raise HTTPException(503, f"Vital-sign analysis is not available: {st['reason']}")
    if Path(file.filename or "").suffix.lower() not in settings.allowed_video_extensions:
        raise HTTPException(422, f"Unsupported file type. Allowed: {', '.join(settings.allowed_video_extensions)}")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="vitals-") as tmp:
        dest = Path(tmp) / f"video{Path(file.filename).suffix.lower()}"
        _save_video(dest, file)
        try:
            detail = get_analyzer().analyze(str(dest))
        except (VitalsUnavailable, ValueError) as e:
            raise HTTPException(422 if isinstance(e, ValueError) else 503, str(e)) from e
    ctx = Context("adhoc", {}, load_rules(db))
    ctx.obs = {"vitals_detail": detail}
    r = vitals_rule(ctx)
    return {"filename": file.filename, "status": r["status"], "confidence": r["confidence"], "summary": r["summary"],
            "exceptions": r["exceptions"], "details": r["details"]}


@router.post("/age/analyze", dependencies=REVIEWER)
def analyze_age(file: UploadFile = File(...), declared_age: str = Form(""), dob: str = Form(""), db: Db = Depends(get_db)):
    """Estimate the age of the person in one face video and return the result immediately (nothing is stored).
    Optionally compare with a declared age or date of birth. For case videos the same test runs automatically."""
    st = age_status()
    if not st["ready"]:
        raise HTTPException(503, f"Age estimation is not available: {st['reason']}")
    if Path(file.filename or "").suffix.lower() not in settings.allowed_video_extensions:
        raise HTTPException(422, f"Unsupported file type. Allowed: {', '.join(settings.allowed_video_extensions)}")
    declared = _declared(declared_age, dob)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="age-") as tmp:
        dest = Path(tmp) / f"video{Path(file.filename).suffix.lower()}"
        _save_video(dest, file)
        try:
            detail = get_age_analyzer().analyze(str(dest))
        except (AgeUnavailable, ValueError) as e:
            raise HTTPException(422 if isinstance(e, ValueError) else 503, str(e)) from e
    ctx = Context("adhoc", {}, load_rules(db), declared=declared)
    ctx.obs = {"age_detail": detail}
    r = age_rule(ctx)
    return {"filename": file.filename, "status": r["status"], "confidence": r["confidence"], "summary": r["summary"],
            "exceptions": r["exceptions"], "details": r["details"]}


class AnalyzeIn(BaseModel):
    tests: list[str] | None = None  # test keys; omitted = every test


def _only(tests: list[str] | None) -> list[str] | None:
    """Validate the tests to re-run: known keys, each engine ready (so a good result is never replaced by 'Unavailable')."""
    if not tests:
        return None
    unknown = [t for t in tests if t not in parameters.BY_KEY]
    if unknown:
        raise HTTPException(422, f"Unknown test: {', '.join(unknown)}")
    tests = list(dict.fromkeys(tests))
    if set(tests) == set(parameters.BY_KEY):
        return None
    for t in tests:
        st = parameters.engine_status_fn(parameters.BY_KEY[t])()
        if not st.get("ready"):
            raise HTTPException(503, f"{parameters.BY_KEY[t]['label']} is not available: {st.get('reason')}")
    return tests


@router.post("/cases/{case_id}/analyze", dependencies=REVIEWER)
def reanalyze(case_id: int, body: AnalyzeIn | None = None, user: Row = Depends(A.require_role("reviewer")), db: Db = Depends(get_db)):
    """Re-run every test, or just the ones in `tests`; the others keep their results."""
    c = _get(db, case_id)
    only = _only(body.tests if body else None)
    if c.status == "PROCESSING" or jobs.is_busy(c.id):
        raise HTTPException(409, "Case is already queued or being processed")
    if only and c.status != "ANALYZED":
        raise HTTPException(409, "Run the full analysis first; single tests can only be re-run on an analysed video.")
    if only:
        activity.case_event(db, c, user.username, "re-run requested", {"tests": only, "decision": c.decision})
    c.status = "PENDING"
    db.save(c)
    run = runs.create(db, c, only, user.username)
    db.commit()
    jobs.enqueue(c.id, only, run.id)
    return _summary(c)


class BulkIn(BaseModel):
    action: Literal["delete", "rerun"]
    ids: list[int] = Field(min_length=1, max_length=500)
    tests: list[str] | None = None  # for a re-run: only these tests (omitted = all)


def _delete_case(db: Db, c: Row, who: str) -> None:
    """Remove a case, its results and every stored file. Raises on a storage error so nothing is silently orphaned."""
    media = dict(c.media or {})
    blob_uris = [u for u in (media.get("video"), media.get("preview"), media.get("id_document")) if storage.is_blob(u)]
    if blob_uris:
        store = storage.get_store(db)
        if store is None:
            raise RuntimeError("stored in blob storage, but no Azure Blob Storage connection is active to delete it")
        for u in blob_uris:
            store.delete_blob(u)
            storage.drop_cached(u)
        store.delete_folder_marker(blob_uris[0])  # the now-empty case folder
    local = media.get("video")
    if local and not storage.is_blob(local):
        storage.remove_staging(Path(local))
    db.insert("deletion_log", case_ref=c.case_ref, filename=media.get("original_name") or c.case_ref,
              reference=c.proposal_no if c.proposal_no != "-" else None, decision=c.decision,
              final_decision=c.final_decision, stored_in="blob" if blob_uris else "local", deleted_by=who)
    activity.record(db, who, "video", "deleted", c.case_ref, {"filename": media.get("original_name") or c.case_ref, "decision": c.decision,
                                                           "final_decision": c.final_decision, "stored_in": "blob" if blob_uris else "local"})
    runs.cancel_open(db, c.id, "The video was deleted")
    for table in ("audit_log", "case_params", "case_exceptions"):
        db.delete(table, "case_id = ?", (c.id,))
    db.delete("cases", "id = ?", (c.id,))
    db.commit()


@router.post("/cases/bulk")
def bulk_action(body: BulkIn, user: Row = Depends(A.require_role("reviewer")), db: Db = Depends(get_db)):
    """Delete or re-run several videos at once. Per-video outcomes are returned; one failure does not stop the rest."""
    ids = list(dict.fromkeys(body.ids))
    if body.action == "delete":
        if A.RANK[user.role] < A.RANK["admin"]:  # destructive: administrators only
            raise HTTPException(403, "Deleting videos needs the admin role.")
    else:
        st = engine_status()
        if not st["ready"]:
            raise HTTPException(503, f"Analysis models are not available: {st['reason']}")
    only = _only(body.tests) if body.action == "rerun" else None
    results = []
    cases = {c.id: c for c in db.all(f"SELECT * FROM {T.cases} WHERE id IN ({marks(len(ids))})", ids, "cases")}
    for i in ids:
        c = cases.get(i)
        if c is None:
            results.append({"id": i, "ok": False, "message": "Not found"})
            continue
        # busy = queued, being analysed, or still copying to blob storage afterwards; a delete or re-run would collide with it
        busy = c.status == "PROCESSING" or (jobs.is_busy(c.id) and (body.action == "rerun" or not jobs.cancel(c.id)))
        if busy:
            results.append({"id": i, "ok": False, "case_ref": c.case_ref, "message": "Being analysed right now; try again in a moment"})
            continue
        try:
            if body.action == "delete":
                _delete_case(db, c, user.username)
            else:
                if only and c.status != "ANALYZED":
                    results.append({"id": i, "ok": False, "case_ref": c.case_ref, "message": "Not analysed yet: run all tests first"})
                    continue
                prev = {"decision": c.decision, "final_decision": c.final_decision, "reviewer": c.reviewer,
                        "review_status": c.review_status, **({"tests": only} if only else {})}
                c.status = "PENDING"
                db.save(c)
                activity.case_event(db, c, user.username, "re-run requested", prev)
                run = runs.create(db, c, only, user.username)
                db.commit()
                jobs.enqueue(c.id, only, run.id)
            results.append({"id": i, "ok": True, "case_ref": c.case_ref})
        except Exception as e:  # noqa: BLE001 - report per video
            db.rollback()
            results.append({"id": i, "ok": False, "case_ref": cases[i].case_ref if i in cases else None, "message": str(e)[:200]})
    ok = sum(1 for r in results if r["ok"])
    return {"action": body.action, "succeeded": ok, "failed": len(results) - ok, "results": results}


class ReviewIn(BaseModel):
    decision: str
    notes: str = ""


@router.post("/cases/{case_id}/review", dependencies=REVIEWER)
def review_case(case_id: int, body: ReviewIn, user: Row = Depends(A.require_role("reviewer")), db: Db = Depends(get_db)):
    """Human-in-the-loop: the authorised reviewer records the final business decision."""
    if body.decision not in DECISIONS - {"Needs Review"}:
        raise HTTPException(422, "decision must be Accept or Reject")
    c = _get(db, case_id)
    if c.status != "ANALYZED":
        raise HTTPException(409, "Case has not been analyzed")
    c.final_decision, c.reviewer, c.review_notes = body.decision, user.username, body.notes
    c.review_status, c.reviewed_at = "COMPLETED", datetime.now(timezone.utc)
    db.save(c)
    activity.case_event(db, c, user.username, "reviewed", {"ai_decision": c.decision, "final_decision": body.decision, "notes": body.notes})
    db.commit()
    return _detail(c, db)


@router.get("/dashboard")
def dashboard(days: int = Query(30, ge=0, le=365), db: Db = Depends(get_db)):
    """Aggregates only, with a previous-period comparison for every figure. days=1 -> hourly buckets, days=0 -> all time."""
    return build_dashboard(db, days)


@router.get("/config")
def get_config(db: Db = Depends(get_db)):
    return load_rules(db)


@router.put("/config")
def put_config(body: dict, me: Row = Depends(A.require_role("admin")), db: Db = Depends(get_db)):
    """Kept for existing callers; the Rules page uses PUT /api/rules (which also takes a note)."""
    if not body:
        return load_rules(db)
    return save_with_audit(db, me, body)["rules"]


@router.get("/reports/export.csv")
def export_csv(db: Db = Depends(get_db)):
    """Every analysed video, all columns. The Reports page offers filters, a column choice and PDF."""
    def gen():
        with open_db() as own:
            yield from reportgen.stream_csv(own, reportgen.Filters(), list(reportgen.LEGACY), machine_header=True)

    return StreamingResponse(gen(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=pivc_authenticity_reports.csv"})


@router.get("/cases/{case_id}/report")
def download_report(case_id: int, db: Db = Depends(get_db)):
    c = _get(db, case_id)
    if not c.report:
        raise HTTPException(409, "No report yet")
    return StreamingResponse(iter([json.dumps(_detail(c, db), default=str, indent=2)]), media_type="application/json",
                             headers={"Content-Disposition": f"attachment; filename={c.case_ref}.json"})


# ---- history of test runs (what is being processed now and what ran before) ---------------------------------------------
@router.get("/history")
def run_history(status: str = Query("", pattern="^(|active|done|failed)$"), test: str = "", days: int = Query(0, ge=0),
                page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                user: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    """Every analysis that was requested, newest first, with the ones in progress on top. Speaks in test names only:
    which engine or model did the work is not part of it."""
    if test and test not in parameters.BY_KEY:
        raise HTTPException(422, f"Unknown test: {test}")
    sql, args = "1 = 1", []
    if status == "active":
        sql += " AND r.status IN ('QUEUED', 'RUNNING')"
    elif status == "done":
        sql += " AND r.status = 'DONE'"
    elif status == "failed":
        sql += " AND r.status IN ('FAILED', 'CANCELLED')"
    if test:  # a run covers the test if it ran everything or named it
        sql, args = sql + " AND (r.tests IS NULL OR r.tests LIKE ? ESCAPE '!')", args + [like(f'"{test}"')]
    if days:
        sql, args = sql + " AND r.requested_at >= ?", args + [datetime.now(timezone.utc) - timedelta(days=days)]
    total = db.scalar(f"SELECT COUNT(*) FROM {T.runs} r WHERE {sql}", args)
    counts = {r.status: r.n for r in db.all(f"SELECT status, COUNT(*) AS n FROM {T.runs} WHERE status IN ('QUEUED', 'RUNNING') GROUP BY status")}
    rows = db.all(
        f"SELECT r.*, (SELECT COUNT(*) FROM {T.runs} q WHERE q.status = 'QUEUED' AND q.id < r.id) AS ahead, "
        f"CASE WHEN EXISTS (SELECT 1 FROM {T.cases} c WHERE c.id = r.case_id) THEN 1 ELSE 0 END AS video_exists "
        f"FROM {T.runs} r WHERE {sql} "
        f"ORDER BY CASE r.status WHEN 'RUNNING' THEN 0 WHEN 'QUEUED' THEN 1 ELSE 2 END, CASE WHEN r.status = 'QUEUED' THEN r.id END ASC, r.id DESC "
        f"OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", [*args, (page - 1) * page_size, page_size], "runs")
    label = {k: p["label"] for k, p in parameters.BY_KEY.items()}
    order = list(parameters.BY_KEY)
    is_admin = A.RANK[user.role] >= A.RANK["admin"]
    items = []
    for r in rows:
        results = [{"test": label[k], "key": k, "from": (r.before or {}).get(k), "to": (r.after or {}).get(k)}
                   for k in order if r.after and k in r.after]
        items.append({
            "id": r.id, "case_id": r.case_id if r.video_exists else None, "case_ref": r.case_ref, "filename": r.filename, "kind": r.kind,
            "tests": [label.get(k, k) for k in r.tests] if r.tests else ["All tests"],
            "requested_by": r.requested_by, "requested_at": r.requested_at, "started_at": r.started_at, "finished_at": r.finished_at,
            "status": r.status, "ahead": r.ahead if r.status == "QUEUED" else None,
            "duration_ms": max(0, int((r.finished_at - r.started_at).total_seconds() * 1000)) if r.finished_at and r.started_at else None,
            "results": results, "decision_before": r.decision_before, "decision_after": r.decision_after,
            "error": (r.error if (is_admin or r.status == "CANCELLED") else ("The run could not be completed." if r.status == "FAILED" else None)),
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items,
            "active": {"queued": counts.get("QUEUED", 0), "running": counts.get("RUNNING", 0)}}


# ---- AI interpretation and the PDF report of one video ------------------------------------------------------------------
@router.get("/ai/status")
def ai_status(db: Db = Depends(get_db)):
    """Whether an OpenAI connection is active (so the page can offer the AI features). Nothing about it is revealed."""
    return {"configured": llm.is_configured(db)}


@router.post("/cases/{case_id}/interpretation", dependencies=REVIEWER)
def make_interpretation(case_id: int, user: Row = Depends(A.require_role("reviewer")), db: Db = Depends(get_db)):
    """Write (or rewrite) the plain-language interpretation of this video's results."""
    c = _get(db, case_id)
    if c.status != "ANALYZED" or not c.report:
        raise HTTPException(409, "The video has not been analysed yet.")
    try:
        interpret.generate(db, c)
    except llm.NotConfigured as e:
        raise HTTPException(503, str(e)) from e
    except llm.LlmError as e:
        raise HTTPException(502, str(e)) from e
    activity.case_event(db, c, user.username, "interpretation generated", {})
    db.commit()
    return interpret.view(c)


@router.get("/cases/{case_id}/report.pdf")
def case_report_pdf(case_id: int, include_ai: bool = True, user: Row = Depends(A.current_user), db: Db = Depends(get_db)):
    """A printable report of one video: decision, interpretation, each test, review, rules and activity."""
    c = _get(db, case_id)
    if not c.report:
        raise HTTPException(409, "No report yet")
    audit = db.all(f"SELECT * FROM {T.audit_log} WHERE case_id = ? ORDER BY id", (c.id,), "audit_log")
    name = (c.media or {}).get("original_name") or c.case_ref
    pdf = casepdf.build(c, audit, interpret.view(c) if include_ai else None, user.full_name or user.username, name)
    activity.record(db, user.username, "reports", "video report downloaded", c.case_ref, {"filename": name}, c.id)
    db.commit()
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={c.case_ref}.pdf"})
