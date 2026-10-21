"""Liveness service: does this image or video show a real, present face rather than a photo, screen or mask?

Independent FastAPI service. Passive, single-frame anti-spoofing (MiniFASNet) on images or sampled video frames.
It is a RISK SIGNAL for recorded footage, not proof: see `limitations`. Nothing is stored and image content is never logged.
"""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from . import media
from .config import Settings, get_settings
from .decision import aggregate
from .engine import LivenessEngine, ModelsMissing
from .security import require_api_key

log = logging.getLogger("liveness")
VERSION = "1.0.0"
LIMITATIONS = [
    "Passive single-frame model: it can miss high-quality screen replays, silicone or 3D masks, and deepfake video. It is not proof of liveness.",
    "Trained on a limited set of cameras; accuracy on compressed video-call footage is unmeasured here. Calibrate on your own genuine and attack samples.",
    "Needs the face with surrounding context; tightly cropped ID-style photos are flagged 'tight_framing' and are not judged.",
    "For bank-grade liveness use an active, capture-time check (challenge-response SDK) instead of, or in addition to, this.",
]


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class ImageOut(Loose):
    decision: str
    spoof_suspected: bool
    real_probability: float | None
    reason: str
    faces_found: int
    face: dict | None
    thresholds: dict
    limitations: list[str]


class VideoOut(Loose):
    decision: str
    spoof_suspected: bool
    real_probability: float | None
    reason: str
    frames: list[dict]
    thresholds: dict
    limitations: list[str]


def _thresholds(s: Settings, pass_t: float | None, spoof_t: float | None) -> tuple[float, float]:
    p, sp = s.pass_threshold if pass_t is None else pass_t, s.spoof_threshold if spoof_t is None else spoof_t
    if not (0.0 <= sp < p <= 1.0):
        raise HTTPException(422, "Thresholds must satisfy 0 <= spoof_threshold < pass_threshold <= 1.")
    return p, sp


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not s.disable_auth and not s.keys:
            raise RuntimeError("No API key configured. Set LIVENESS_API_KEYS (comma separated), or LIVENESS_DISABLE_AUTH=true for local development only.")
        try:
            app.state.engine = LivenessEngine(s)
        except ModelsMissing as e:
            raise RuntimeError(str(e)) from e
        log.info("liveness service ready")
        yield

    app = FastAPI(title="Liveness Service", version=VERSION, lifespan=lifespan,
                  description="Passive face anti-spoofing with MiniFASNet. Send your key in the X-API-Key header.")
    app.state.settings = s

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    engine = lambda req: req.app.state.engine  # noqa: E731
    auth = [Depends(require_api_key)]

    @app.get("/health")
    def health(request: Request):
        return {"status": "ok" if getattr(request.app.state, "engine", None) else "starting", "version": VERSION}

    @app.get("/v1/info", dependencies=auth)
    def info(request: Request):
        return {"service": "liveness", "version": VERSION, "models": engine(request).info(), "detector": "YuNet (OpenCV Zoo)",
                "thresholds": {"pass": s.pass_threshold, "spoof": s.spoof_threshold},
                "limits": {"max_image_mb": s.max_image_mb, "max_video_mb": s.max_video_mb, "max_frames": s.max_frames},
                "licences": {"minifasnet": "Apache-2.0 (Minivision, Silent-Face-Anti-Spoofing)", "yunet": "MIT (OpenCV Zoo)"},
                "limitations": LIMITATIONS}

    @app.post("/v1/liveness/image", response_model=ImageOut, dependencies=auth)
    def check_image(request: Request, image: UploadFile = File(...), pass_threshold: float | None = Form(None),
                    spoof_threshold: float | None = Form(None)):
        """Score the main face in one image."""
        p, sp = _thresholds(s, pass_threshold, spoof_threshold)
        img = media.decode_image(image, s.max_image_mb)
        an = engine(request).analyze(img)
        face = an["face"]
        row = {"face": face is not None, "real_probability": face["real_probability"] if face else None, "flags": face["flags"] if face else []}
        agg = aggregate([row], p, sp, 1)
        return {"decision": agg["decision"], "spoof_suspected": agg["spoof_suspected"], "real_probability": agg["real_probability"],
                "reason": agg["reason"], "faces_found": an["faces_found"], "face": face, "thresholds": {"pass": p, "spoof": sp},
                "limitations": LIMITATIONS}

    @app.post("/v1/liveness/video", response_model=VideoOut, dependencies=auth)
    def check_video(request: Request, video: UploadFile = File(...), frames: int | None = Form(None),
                    pass_threshold: float | None = Form(None), spoof_threshold: float | None = Form(None)):
        """Score evenly spaced frames of a video and combine them (median over the usable frames)."""
        p, sp = _thresholds(s, pass_threshold, spoof_threshold)
        n = s.default_frames if frames is None else frames
        if not 1 <= n <= s.max_frames:
            raise HTTPException(422, f"frames must be between 1 and {s.max_frames}.")
        eng, rows = engine(request), []
        with media.saved_video(video, s.max_video_mb) as path:
            for idx, secs, frame in media.sample_frames(path, n):
                an = eng.analyze(frame)
                f = an["face"]
                rows.append({"frame": idx, "seconds": secs, "face": f is not None, "faces_found": an["faces_found"],
                             "real_probability": f["real_probability"] if f else None, "label": f["label"] if f else None,
                             "flags": f["flags"] if f else []})
        agg = aggregate(rows, p, sp, s.min_usable_frames)
        return {**agg, "frames": rows, "thresholds": {"pass": p, "spoof": sp}, "limitations": LIMITATIONS}

    return app


app = create_app()
