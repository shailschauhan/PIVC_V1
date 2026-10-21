"""Service and engine health: what each test's engine is, whether it is ready, and how it has behaved on real videos.

Every run of an engine on a video is recorded (engine_calls) by `track`; the numbers on the health page are computed from
those rows, so latency and error rate describe real work, not synthetic pings. Readiness comes from the engine itself
(for the two services, a live probe).
"""
import importlib.metadata
import logging
import random
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


from . import parameters
from .config import settings
from .db import T, Db, open_db

log = logging.getLogger(__name__)
KEEP_DAYS = 30


@contextmanager
def track(engine: str):
    """Time one engine run. An exception means the engine failed; it is re-raised unchanged for the caller to handle."""
    t0, err = time.perf_counter(), None
    try:
        yield
    except Exception as e:  # noqa: BLE001 - recorded, then re-raised
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        _write(engine, err is None, int((time.perf_counter() - t0) * 1000), err)


def _write(engine: str, ok: bool, ms: int, error: str | None) -> None:
    try:
        with open_db() as db:
            db.insert("engine_calls", engine=engine, ok=ok, ms=ms, error=(error or "")[:300] or None)
            if random.random() < 0.002:  # now and then, drop what is too old to matter
                db.delete("engine_calls", "at < ?", (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS),))
            db.commit()
    except Exception:  # noqa: BLE001 - monitoring must never break an analysis
        log.exception("could not record engine call")


def _pctl(sorted_vals: list[int], q: float) -> int | None:
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def stats(db: Db, engine: str, hours: int = 24, buckets: int = 24) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    rows = db.all(f"SELECT TOP 100000 at, ok, ms, error FROM {T.engine_calls} WHERE engine = ? AND at >= ? ORDER BY at", (engine, since), "engine_calls")
    ok_ms = sorted(r.ms for r in rows if r.ok)
    errors = [r for r in rows if not r.ok]
    width = timedelta(hours=hours) / buckets
    series = [{"at": (since + width * i).isoformat(), "calls": 0, "errors": 0, "_ms": []} for i in range(buckets)]
    for r in rows:
        b = series[min(buckets - 1, int((r.at - since) / width))]
        b["calls"] += 1
        b["errors"] += not r.ok
        if r.ok:
            b["_ms"].append(r.ms)
    for b in series:
        ms = sorted(b.pop("_ms"))
        b["p50_ms"] = _pctl(ms, 0.5)
    last_ok = db.one(f"SELECT TOP 1 at FROM {T.engine_calls} WHERE engine = ? AND ok = 1 ORDER BY at DESC", (engine,), "engine_calls")
    last_err = db.one(f"SELECT TOP 1 at, error FROM {T.engine_calls} WHERE engine = ? AND ok = 0 ORDER BY at DESC", (engine,), "engine_calls")
    return {
        "hours": hours, "calls": len(rows), "errors": len(errors), "error_rate": (len(errors) / len(rows)) if rows else None,
        "p50_ms": _pctl(ok_ms, 0.5), "p95_ms": _pctl(ok_ms, 0.95), "max_ms": ok_ms[-1] if ok_ms else None,
        "last_success": last_ok.at if last_ok else None,
        "last_error": {"at": last_err.at, "message": last_err.error} if last_err else None,
        "series": series,
        "recent_errors": [{"at": r.at, "message": r.error} for r in errors[-15:][::-1]],
    }


def _version(pkg: str) -> str | None:
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return None


def describe(key: str, status: dict) -> dict:
    """Static facts about an engine: how it runs, its version and non-secret settings. `status` is its live status."""
    spec = parameters.BY_KEY[key]
    base = {"key": key, "label": spec["label"], "engine": spec["engine"], "question": spec["question"]}
    if key == "video_authenticity":
        return {**base, "kind": "In this process", "version": f"torch {_version('torch')}, timm {_version('timm')}",
                "models": status.get("models") or [], "settings": {"Dataset": status.get("dataset"), "Frames per video": settings.authenticity_fpv}}
    if key == "health_indicators":
        return {**base, "kind": "In this process", "version": f"open-rppg {_version('open-rppg')}", "models": [status.get("model")] if status.get("model") else [],
                "settings": {"Enabled": settings.vitals_enabled}}
    if key == "age_estimation":
        return {**base, "kind": "Separate worker process", "version": status.get("model"), "models": [status.get("model")] if status.get("model") else [],
                "settings": {"Enabled": settings.age_enabled, "Frames per video": settings.age_frames}}
    info = status.get("info") or {}
    url, frames = (settings.face_match_url, settings.face_match_frames) if key == "face_match" else (settings.liveness_url, settings.liveness_frames)
    return {**base, "kind": "Separate service (own API)", "version": info.get("version"),
            "models": [m if isinstance(m, str) else m.get("name", str(m)) for m in (info.get("models") if isinstance(info.get("models"), list) else [])],
            "settings": {"Address": url or "not configured", "Frames per video": frames, "Timeout (s)": settings.services_timeout_s}}


def overview(db: Db, force: bool = False) -> list[dict]:
    """One entry per engine: static facts + live readiness + 24 h behaviour."""
    from concurrent.futures import ThreadPoolExecutor

    def live(p):
        fn = parameters.engine_status_fn(p)
        return fn(force) if p["key"] in ("face_match", "liveness") else fn()

    with ThreadPoolExecutor(max_workers=len(parameters.PARAMETERS)) as pool:
        states = list(pool.map(live, parameters.PARAMETERS))
    out = []
    for p, st in zip(parameters.PARAMETERS, states):
        s = stats(db, p["key"], 24)
        out.append({**describe(p["key"], st), "ready": bool(st.get("ready")), "reason": st.get("reason"),
                    "probe_ms": st.get("latency_ms"), "stats": s})
    return out
