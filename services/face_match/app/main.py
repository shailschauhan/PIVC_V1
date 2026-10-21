"""Face-match service: is the person in these two images (or in this video and this ID photo) the same person?

Independent FastAPI service (YuNet detector + SFace recogniser via OpenCV). Nothing is stored: uploads live in memory or in a
temporary file that is deleted when the request ends, and image content is never logged.
"""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from . import media
from .config import Settings, get_settings
from .decision import aggregate_frames, decide_pair
from .engine import FaceEngine, ModelsMissing
from .security import require_api_key

log = logging.getLogger("face_match")
VERSION = "1.0.0"
LIMITATIONS = [
    "Similarity scores are uncalibrated: choose thresholds from your own ID-photo/selfie pairs before relying on a decision.",
    "A 'mismatch' is never reported when image quality is poor; it becomes 'review'.",
    "Not a liveness check: a photo of a person matches like the person does.",
]


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class FaceOut(Loose):
    box: list[float]
    score: float
    quality: dict


class DetectOut(Loose):
    faces: list[FaceOut]
    faces_found: int
    image_size: list[int]


class CompareOut(Loose):
    decision: str
    similarity: float | None
    reason: str
    thresholds: dict
    faces_found: dict
    quality: dict
    model: str
    limitations: list[str]


class VideoOut(Loose):
    decision: str
    similarity: float | None
    reason: str
    thresholds: dict
    frames: list[dict]
    reference: dict
    model: str
    limitations: list[str]


def _thresholds(s: Settings, match: float | None, mismatch: float | None) -> tuple[float, float]:
    m, mm = s.match_threshold if match is None else match, s.mismatch_threshold if mismatch is None else mismatch
    if not (0.0 <= mm < m <= 1.0):
        raise HTTPException(422, "Thresholds must satisfy 0 <= mismatch_threshold < match_threshold <= 1.")
    return m, mm


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not s.disable_auth and not s.keys:
            raise RuntimeError("No API key configured. Set FACE_MATCH_API_KEYS (comma separated), or FACE_MATCH_DISABLE_AUTH=true for local development only.")
        try:
            app.state.engine = FaceEngine(s)
        except ModelsMissing as e:
            raise RuntimeError(str(e)) from e
        log.info("face-match service ready")
        yield

    app = FastAPI(title="Face Match Service", version=VERSION, lifespan=lifespan,
                  description="Compares faces with YuNet + SFace. Send your key in the X-API-Key header.")
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
        return {"service": "face-match", "version": VERSION, "models": engine(request).info(),
                "thresholds": {"match": s.match_threshold, "mismatch": s.mismatch_threshold},
                "quality_rules": {"min_face_px": s.min_face_px, "min_sharpness": s.min_sharpness, "max_yaw": s.max_yaw},
                "limits": {"max_image_mb": s.max_image_mb, "max_video_mb": s.max_video_mb, "max_frames": s.max_frames},
                "licences": {"yunet": "MIT (OpenCV Zoo)", "sface": "Apache-2.0 (OpenCV Zoo)"}, "limitations": LIMITATIONS}

    @app.post("/v1/faces/detect", response_model=DetectOut, dependencies=auth)
    def detect(request: Request, image: UploadFile = File(...)):
        """Find faces in one image: boxes, landmarks and quality measurements (no identity comparison)."""
        img = media.decode_image(image, s.max_image_mb)
        faces = engine(request).describe_all(img)
        return {"faces": faces, "faces_found": len(faces), "image_size": [img.shape[1], img.shape[0]]}

    @app.post("/v1/faces/compare", response_model=CompareOut, dependencies=auth)
    def compare(request: Request, image_a: UploadFile = File(...), image_b: UploadFile = File(...),
                match_threshold: float | None = Form(None), mismatch_threshold: float | None = Form(None)):
        """Compare the main face in image_a with the main face in image_b (e.g. a selfie and the photo on an ID card)."""
        m, mm = _thresholds(s, match_threshold, mismatch_threshold)
        a, b = media.decode_image(image_a, s.max_image_mb), media.decode_image(image_b, s.max_image_mb)
        eng = engine(request)
        an_a, an_b = eng.analyze(a), eng.analyze(b)
        sim = eng.similarity(an_a.feature, an_b.feature) if an_a.face and an_b.face else None
        fa = an_a.quality["flags"] if an_a.quality else []
        fb = an_b.quality["flags"] if an_b.quality else []
        return {**decide_pair(sim, m, mm, fa, fb), "similarity": None if sim is None else round(sim, 4),
                "thresholds": {"match": m, "mismatch": mm}, "faces_found": {"a": an_a.faces_found, "b": an_b.faces_found},
                "quality": {"a": an_a.quality, "b": an_b.quality}, "model": "YuNet + SFace (cosine)", "limitations": LIMITATIONS}

    @app.post("/v1/faces/compare-video", response_model=VideoOut, dependencies=auth)
    def compare_video(request: Request, video: UploadFile = File(...), reference: UploadFile = File(...),
                      frames: int | None = Form(None), match_threshold: float | None = Form(None),
                      mismatch_threshold: float | None = Form(None)):
        """Compare faces from evenly spaced video frames with a reference photo (e.g. the ID document photo)."""
        m, mm = _thresholds(s, match_threshold, mismatch_threshold)
        n = s.default_frames if frames is None else frames
        if not 1 <= n <= s.max_frames:
            raise HTTPException(422, f"frames must be between 1 and {s.max_frames}.")
        ref_img = media.decode_image(reference, s.max_image_mb)
        eng = engine(request)
        ref = eng.analyze(ref_img)
        rows: list[dict] = []
        with media.saved_video(video, s.max_video_mb) as path:
            for idx, secs, frame in media.sample_frames(path, n):
                an = eng.analyze(frame)
                row = {"frame": idx, "seconds": secs, "face": an.face is not None, "faces_found": an.faces_found,
                       "similarity": None, "quality_flags": an.quality["flags"] if an.quality else []}
                if an.face and ref.face:
                    row["similarity"] = round(eng.similarity(an.feature, ref.feature), 4)
                rows.append(row)
        ref_flags = ref.quality["flags"] if ref.quality else []
        agg = aggregate_frames(rows, m, mm, ref_flags, s.min_usable_frames)
        return {**agg, "thresholds": {"match": m, "mismatch": mm}, "frames": rows,
                "reference": {"faces_found": ref.faces_found, "quality": ref.quality},
                "model": "YuNet + SFace (cosine)", "limitations": LIMITATIONS}

    return app


app = create_app()
