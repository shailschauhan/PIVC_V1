import logging
import re
from pathlib import Path
import time
from datetime import datetime, timezone


from .analyzers.base import Context
from .analyzers.checks import ANALYZERS
from .analyzers.providers import get_provider
from .decision import decide
from . import storage
from .media import ensure_preview
from . import activity, runs
from .db import T, Db, Row, marks
from .rules import load_rules

log = logging.getLogger(__name__)
REPORT_LABELS = {"video_authenticity": "Video Authenticity", "health_indicators": "Health Indicators (rPPG vitals)",
                 "age_estimation": "Age Estimation",
                 "face_match": "Face Match", "liveness": "Liveness"}


def build_report(case: Row, rules: dict, media: dict, only: list[str] | None = None) -> dict:
    """`media` holds LOCAL file paths (blob copies are downloaded first).
    With `only`, just those tests are run again; the others keep their stored results and the decision is recomputed from all of them."""
    provider = get_provider()
    ctx = Context(case.case_ref, media, rules, declared=case.declared or {})
    ctx.obs = provider.extract(ctx, only)
    params = dict(case.report["parameters"]) if only else {}
    for k, fn in ANALYZERS.items():
        if only is None or k in only:
            params[k] = fn(ctx)
            params[k]["engine"] = ctx.obs["engines"][k]
    decision, confidence, exceptions = decide(params, rules)
    return {
        "case_ref": case.case_ref,
        "parameters": params,
        "labels": REPORT_LABELS,
        "overall_decision": decision,
        "confidence_score": confidence,
        "exception_details": exceptions,
        "rules_applied": rules,
    }


def _primary_score(k: str, p: dict) -> float | None:
    if k == "video_authenticity":
        return p["details"].get("fake_score")
    if k == "health_indicators":
        return p["details"].get("hr_bpm")
    if k == "age_estimation":
        return p["details"].get("age")
    if k == "face_match":
        return p["details"].get("similarity")
    return p["details"].get("real_probability") if k == "liveness" else None


def _store_rows(db: Db, case: Row, report: dict, only: list[str] | None = None) -> None:
    """Rewrite the per-test rows the dashboard reads: all of them, or just those of the tests that were re-run."""
    where, args = ("case_id = ?" + (f" AND parameter IN ({marks(len(only))})" if only else ""), [case.id, *(only or [])])
    db.delete("case_params", where, args)
    db.delete("case_exceptions", where, args)
    for k, p in report["parameters"].items():
        if only and k not in only:
            continue
        status = "Estimated age" if p["status"].startswith("Estimated age") else p["status"]
        db.insert("case_params", case_id=case.id, parameter=k, status=status, confidence=p["confidence"],
                  score=_primary_score(k, p), engine=p["engine"])
        for msg in p["exceptions"]:
            reason = re.sub(r"\s+", " ", re.sub(r"\(.*?\)|\d+(\.\d+)?", "", msg)).strip(" :-")[:200]
            db.insert("case_exceptions", case_id=case.id, parameter=k, reason=reason, message=msg)


def analyze_case(db: Db, case_id: int, only: list[str] | None = None, run_id: int | None = None) -> None:
    """Run every test, or only those in `only` (a re-run of single modules, which needs an existing full analysis).
    `run_id` is this run's entry in the history (runs.py)."""
    case = db.get("cases", case_id)
    if not case:
        runs.finish(db, run_id, "CANCELLED", error="The video no longer exists")
        return
    if only and not (case.report and "video_authenticity" in (case.report.get("parameters") or {})):
        only = None  # nothing to merge into: do the whole analysis
    if only:
        return _reanalyze_tests(db, case, only, run_id)
    prev_params, prev_decision = dict((case.report or {}).get("parameters") or {}), case.decision
    case.status, case.error = "PROCESSING", None
    db.save(case)
    db.commit()
    runs.start(db, run_id)
    t0 = time.perf_counter()
    local = None
    try:
        local = storage.materialize(case.media["video"], db)  # downloads from blob storage if needed
        media = {**case.media, "video": str(local)}
        if case.media.get("id_document"):
            try:
                media["id_document"] = str(storage.materialize(case.media["id_document"], db))
            except Exception as e:  # noqa: BLE001 - an unreadable ID photo must not fail the whole analysis
                media["id_document"], media["id_document_error"] = None, f"ID document could not be loaded: {e}"
        report = build_report(case, load_rules(db), media)
        case.report = report
        case.decision = report["overall_decision"]
        case.confidence = report["confidence_score"]
        case.status = "ANALYZED"
        case.analyzed_at = datetime.now(timezone.utc)
        # Needs Review is routed to manual verification; other outcomes are final unless overridden
        needs_review = case.decision == "Needs Review"
        case.review_status = "PENDING" if needs_review else "NOT_REQUIRED"
        case.final_decision = None if needs_review else case.decision
        db.save(case)
        _store_rows(db, case, report)
        activity.case_event(db, case, "system", "analyzed", {"decision": case.decision, "confidence": case.confidence})
    except Exception as e:  # noqa: BLE001
        db.rollback()
        case = db.get("cases", case_id)  # back to what was stored
        case.status, case.error = "FAILED", str(e)
    case.processing_ms = int((time.perf_counter() - t0) * 1000)
    db.save(case)
    db.commit()
    if case.status == "ANALYZED":
        runs.finish(db, run_id, "DONE", before={k: p["status"] for k, p in prev_params.items()},
                    after={k: p["status"] for k, p in case.report["parameters"].items()}, decision_before=prev_decision, decision_after=case.decision)
    else:
        runs.finish(db, run_id, "FAILED", error=case.error)
    if local is not None and case.status == "ANALYZED":
        _persist_media(db, case, local)


def _reanalyze_tests(db: Db, case: Row, only: list[str], run_id: int | None = None) -> None:
    """Re-run some tests on an already analysed video. A failure leaves the earlier results exactly as they were."""
    old = case.report["parameters"]
    before, old_decision = {k: old[k]["status"] for k in only if k in old}, case.decision
    case.status = "PROCESSING"
    db.save(case)
    db.commit()
    runs.start(db, run_id)
    local = None
    try:
        local = storage.materialize(case.media["video"], db)
        media = {**case.media, "video": str(local)}
        if case.media.get("id_document"):
            try:
                media["id_document"] = str(storage.materialize(case.media["id_document"], db))
            except Exception as e:  # noqa: BLE001
                media["id_document"], media["id_document_error"] = None, f"ID document could not be loaded: {e}"
        report = build_report(case, load_rules(db), media, only)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        case = db.get("cases", case.id)
        case.status = "ANALYZED"
        db.save(case)
        activity.case_event(db, case, "system", "re-run failed", {"tests": only, "error": str(e)[:300]})
        db.commit()
        runs.finish(db, run_id, "FAILED", before=before, error=str(e))
        return
    case.report, case.decision, case.confidence = report, report["overall_decision"], report["confidence_score"]
    if case.decision != old_decision:  # a different outcome restarts the review clock, as a full analysis would
        needs_review = case.decision == "Needs Review"
        case.review_status = "PENDING" if needs_review else "NOT_REQUIRED"
        case.final_decision = None if needs_review else case.decision
        case.reviewer = case.reviewed_at = case.review_notes = None
        case.analyzed_at = datetime.now(timezone.utc)
    case.status = "ANALYZED"
    db.save(case)
    _store_rows(db, case, report, only)
    activity.case_event(db, case, "system", "re-analyzed", {
        "tests": only, "before": before, "after": {k: report["parameters"][k]["status"] for k in only},
        "decision_before": old_decision, "decision_after": case.decision})
    db.commit()
    runs.finish(db, run_id, "DONE", before=before, after={k: report["parameters"][k]["status"] for k in only},
                decision_before=old_decision, decision_after=case.decision)
    if local is not None:
        _persist_media(db, case, local)


def _persist_media(db: Db, case: Row, local) -> None:
    """Create the playback copy and, when blob storage is on, move the media there. Never fails the analysis:
    on any problem the local files are kept and the reason is recorded on the case."""
    media = dict(case.media)
    try:
        if not storage.is_blob(media.get("preview")):
            try:
                preview = ensure_preview(str(local))
            except Exception as e:  # noqa: BLE001
                log.warning("preview not created for %s: %s", case.case_ref, e)
                preview = None
        else:
            preview = None
        store = storage.get_store(db)
        if store is not None and not storage.is_blob(media["video"]):
            store.ensure_container()
            ext = local.suffix.lower()
            media["video"] = store.put_file(local, f"{case.case_ref}/video{ext}")
            idoc = media.get("id_document")
            if idoc and not storage.is_blob(idoc):  # the ID document photo is personal data: it moves to blob storage with the video
                media["id_document"] = store.put_file(Path(idoc), f"{case.case_ref}/id_document{Path(idoc).suffix.lower()}")
            if preview is not None and preview != local:
                media["preview"] = store.put_file(preview, f"{case.case_ref}/preview.mp4")
            media.pop("storage_error", None)
            case.media = media
            activity.case_event(db, case, "system", "stored in blob storage", {"container": store.container})
            db.save(case)
            db.commit()
            storage.remove_staging(local)  # only after the upload succeeded
        elif preview is not None and preview != local and storage.is_blob(media["video"]) and not media.get("preview"):
            media["preview"] = store.put_file(preview, f"{case.case_ref}/preview.mp4") if store else None
            case.media = {k: v for k, v in media.items() if v}
            db.save(case)
            db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("could not move media of %s to blob storage", case.case_ref)
        case.media = {**case.media, "storage_error": str(e)[:300]}
        activity.case_event(db, case, "system", "blob storage failed", {"error": str(e)[:300]})
        db.save(case)
        db.commit()
