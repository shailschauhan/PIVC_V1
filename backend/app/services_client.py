"""HTTP clients for the two standalone services: Face Match (services/face_match) and Liveness (services/liveness).

Each is an independent FastAPI service reached with a base URL and an API key (FACE_MATCH_URL / FACE_MATCH_API_KEY and
LIVENESS_URL / LIVENESS_API_KEY). If one is not configured or not reachable the test is reported as Unavailable and nothing
else is affected.
"""
import logging
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger(__name__)
STATUS_TTL_S = 30


class ServiceError(RuntimeError):
    pass


class ServiceClient:
    def __init__(self, label: str, engine: str, url: str, api_key: str, enabled: bool, timeout: float):
        self.label, self.engine = label, engine
        self.url, self.key, self.enabled, self.timeout = url.rstrip("/"), api_key, enabled, timeout
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None

    @property
    def _headers(self) -> dict:
        return {"X-API-Key": self.key}

    def status(self, force: bool = False) -> dict:
        """Ready only if the service answers /health AND accepts our key. Cached for a while unless `force`."""
        with self._lock:
            if not force and self._cached and time.time() - self._cached[0] < STATUS_TTL_S:
                return self._cached[1]
        st = self._probe()
        with self._lock:
            self._cached = (time.time(), st)
        return st

    def _probe(self) -> dict:
        base = {"engine": self.engine, "ready": False}
        if not self.enabled:
            return {**base, "reason": "disabled by configuration"}
        if not self.url:
            return {**base, "reason": f"not configured (set {self.label.upper()}_URL and {self.label.upper()}_API_KEY)"}
        t0 = time.perf_counter()
        try:
            h = httpx.get(f"{self.url}/health", timeout=3)
            if h.status_code != 200:
                return {**base, "reason": f"service answered HTTP {h.status_code} on /health"}
            r = httpx.get(f"{self.url}/v1/info", headers=self._headers, timeout=3)
        except httpx.HTTPError as e:
            return {**base, "reason": f"service unreachable at {self.url} ({type(e).__name__})"}
        if r.status_code in (401, 403):
            return {**base, "reason": "service rejected the API key"}
        if r.status_code != 200:
            return {**base, "reason": f"service answered HTTP {r.status_code} on /v1/info"}
        try:
            info = r.json()
        except ValueError:
            info = {}
        return {**base, "ready": True, "reason": "ok", "url": self.url, "info": info,
                "latency_ms": int((time.perf_counter() - t0) * 1000)}

    def post(self, path: str, files: dict[str, Path], data: dict) -> dict:
        """Upload files (streamed from disk) and return the service's JSON. Raises ServiceError with a readable reason."""
        with ExitStack() as stack:
            handles = {k: (p.name, stack.enter_context(p.open("rb"))) for k, p in files.items()}
            try:
                r = httpx.post(f"{self.url}{path}", headers=self._headers, files=handles, data=data, timeout=self.timeout)
            except httpx.HTTPError as e:
                raise ServiceError(f"{self.label} service unreachable ({type(e).__name__})") from e
        if r.status_code == 200:
            return r.json()
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ServiceError(f"{self.label} service error {r.status_code}: {str(detail)[:200]}")


def _make(label: str, engine: str, url: str, key: str, enabled: bool) -> ServiceClient:
    return ServiceClient(label, engine, url, key, enabled, settings.services_timeout_s)


_clients: dict[str, ServiceClient] = {}


def face_match_client() -> ServiceClient:
    if "fm" not in _clients:
        _clients["fm"] = _make("face_match", "SFace (OpenCV)", settings.face_match_url, settings.face_match_api_key, settings.face_match_enabled)
    return _clients["fm"]


def liveness_client() -> ServiceClient:
    if "lv" not in _clients:
        _clients["lv"] = _make("liveness", "MiniFASNet", settings.liveness_url, settings.liveness_api_key, settings.liveness_enabled)
    return _clients["lv"]


def face_match_status(force: bool = False) -> dict:
    return face_match_client().status(force)


def liveness_status(force: bool = False) -> dict:
    return liveness_client().status(force)


def compare_video(video: Path, reference: Path, rules: dict) -> dict:
    return face_match_client().post("/v1/faces/compare-video", {"video": video, "reference": reference}, {
        "frames": settings.face_match_frames, "match_threshold": rules["face_match_threshold"], "mismatch_threshold": rules["face_mismatch_threshold"]})


def check_liveness(video: Path, rules: dict) -> dict:
    return liveness_client().post("/v1/liveness/video", {"video": video}, {
        "frames": settings.liveness_frames, "pass_threshold": rules["liveness_pass_threshold"], "spoof_threshold": rules["liveness_spoof_threshold"]})
