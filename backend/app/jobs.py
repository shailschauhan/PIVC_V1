"""In-process analysis queue.

Uploads return immediately; a small worker pool drains the queue so heavy model inference never runs
on request threads or in parallel beyond `worker_threads`. State lives in the database (cases.status), so
anything still PENDING/PROCESSING after a restart is re-queued on startup. For multi-node scale, replace
this module with Celery/RQ/SQS workers calling pipeline.analyze_case - nothing else needs to change.

A case is *busy* from the moment it is queued until the worker has finished everything for it, including the
post-analysis step that copies the video to blob storage. Deleting or re-running a busy case is refused, so a delete
can never race with that upload (which would leave orphaned blobs or crash the worker).
"""
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from .config import settings
from . import runs
from .db import T, open_db
from .pipeline import analyze_case

log = logging.getLogger(__name__)
_pool: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_active: set[int] = set()          # queued or running
_futures: dict[int, Future] = {}


def _run(case_id: int, only: list[str] | None = None, run_id: int | None = None) -> None:
    try:
        with open_db() as db:
            analyze_case(db, case_id, only, run_id)
    except Exception:  # noqa: BLE001 - never kill the worker
        log.exception("analysis crashed for case %s", case_id)
    finally:
        with _lock:
            _active.discard(case_id)
            _futures.pop(case_id, None)


def _submit(case_id: int, only: list[str] | None = None, run_id: int | None = None) -> None:
    assert _pool is not None, "job queue not started"
    with _lock:
        _active.add(case_id)
        _futures[case_id] = _pool.submit(_run, case_id, only, run_id)


def start() -> None:
    global _pool
    _pool = ThreadPoolExecutor(max_workers=settings.worker_threads, thread_name_prefix="analyzer")
    with open_db() as db:
        for r in db.all(f"SELECT * FROM {T.cases} WHERE status IN ('PENDING', 'PROCESSING') ORDER BY id", (), "cases"):
            runs.cancel_open(db, r.id, "Interrupted by a restart")
            run = runs.create(db, r, None, "system")
            db.commit()
            _submit(r.id, None, run.id)


def stop() -> None:
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)


def enqueue(case_id: int, only: list[str] | None = None, run_id: int | None = None) -> None:
    """`only` limits the run to those tests (keys from parameters.PARAMETERS); `run_id` is its entry in the run history.
    A restart turns a pending partial run into a full one."""
    _submit(case_id, only, run_id)


def is_busy(case_id: int) -> bool:
    """True while the case is queued or being processed (analysis and the blob upload that follows it)."""
    with _lock:
        return case_id in _active


def cancel(case_id: int) -> bool:
    """Take a still-queued case off the queue. Returns True if the case is now idle (cancelled or never busy),
    False if a worker is already running it (it must finish first)."""
    with _lock:
        f = _futures.get(case_id)
        if f is not None and f.cancel():
            _futures.pop(case_id, None)
            _active.discard(case_id)
            return True
        return case_id not in _active


def wait_idle(timeout: float = 30.0) -> bool:
    """Block until nothing is queued or running (used by tests)."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        with _lock:
            if not _active:
                return True
        time.sleep(0.05)
    return False
