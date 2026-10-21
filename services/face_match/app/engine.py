"""Face detection (YuNet) and recognition (SFace) through OpenCV's built-in DNN wrappers. CPU only, no other ML framework."""
import hashlib
import math
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import Settings

YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"
MIN_SIDE, MAX_SIDE = 640, 1600  # images are scaled so their long side falls in this range before detection


class ModelsMissing(RuntimeError):
    pass


@dataclass
class Face:
    row: np.ndarray               # YuNet row (15 floats) in prepared-image coordinates
    score: float

    @property
    def box(self) -> tuple[float, float, float, float]:
        return float(self.row[0]), float(self.row[1]), float(self.row[2]), float(self.row[3])

    @property
    def area(self) -> float:
        return self.box[2] * self.box[3]


@dataclass
class Analysis:
    faces_found: int
    face: Face | None = None
    scale: float = 1.0
    feature: np.ndarray | None = None
    quality: dict | None = None


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FaceEngine:
    def __init__(self, s: Settings):
        self.s = s
        yunet, sface = s.models_dir / YUNET, s.models_dir / SFACE
        missing = [str(p) for p in (yunet, sface) if not p.is_file()]
        if missing:
            raise ModelsMissing(f"Model file(s) not found: {', '.join(missing)}. Run: python scripts/download_models.py")
        self.files = {YUNET: yunet, SFACE: sface}
        self.detector = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320), s.det_score_threshold, 0.3, 50)
        self.recognizer = cv2.FaceRecognizerSF.create(str(sface), "")
        self._lock = threading.Lock()  # the OpenCV wrappers keep per-call state: one inference at a time

    def info(self) -> dict:
        return {name: {"size_mb": round(p.stat().st_size / 1e6, 2), "sha256": sha256(p)} for name, p in self.files.items()}

    # ---- detection ----
    @staticmethod
    def _prepare(img: np.ndarray) -> tuple[np.ndarray, float]:
        long_side = max(img.shape[:2])
        scale = 1.0
        if long_side < MIN_SIDE:
            scale = MIN_SIDE / long_side                 # ID photos are often tiny: upscale so small faces are findable
        elif long_side > MAX_SIDE:
            scale = MAX_SIDE / long_side
        if scale != 1.0:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
        return img, scale

    def detect_all(self, img: np.ndarray) -> tuple[list[Face], np.ndarray, float]:
        prepared, scale = self._prepare(img)
        h, w = prepared.shape[:2]
        with self._lock:
            self.detector.setInputSize((w, h))
            _, rows = self.detector.detect(prepared)
        faces = [Face(r.copy(), float(r[14])) for r in (rows if rows is not None else [])]
        return faces, prepared, scale

    # ---- one image -> subject face, embedding, quality ----
    def analyze(self, img: np.ndarray, with_feature: bool = True) -> Analysis:
        faces, prepared, scale = self.detect_all(img)
        if not faces:
            return Analysis(0)
        face = max(faces, key=lambda f: f.area * f.score)   # the largest, most confident face is the subject
        with self._lock:
            aligned = self.recognizer.alignCrop(prepared, face.row)
            feature = self.recognizer.feature(aligned).copy() if with_feature else None
        return Analysis(len(faces), face, scale, feature, self.quality(aligned, face, scale))

    def quality(self, aligned: np.ndarray, face: Face, scale: float) -> dict:
        r, s = face.row, self.s
        size_px = min(r[2], r[3]) / scale                                   # in the ORIGINAL image's pixels
        sharp = float(cv2.Laplacian(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        (rex, rey), (lex, ley), (nx, _) = (r[4], r[5]), (r[6], r[7]), (r[8], r[9])
        eye = math.hypot(lex - rex, ley - rey) or 1.0
        yaw = (nx - (rex + lex) / 2) / eye                                  # ~0 facing the camera, +-0.5 and beyond = turned away
        roll = math.degrees(math.atan2(ley - rey, lex - rex))
        flags = []
        if size_px < s.min_face_px:
            flags.append("too_small")
        if sharp < s.min_sharpness:
            flags.append("blurry")
        if abs(yaw) > s.max_yaw:
            flags.append("off_angle")
        if face.score < 0.8:
            flags.append("low_detection_score")
        return {"face_size_px": round(float(size_px), 1), "sharpness": round(sharp, 1), "yaw": round(float(yaw), 3),
                "roll_deg": round(float(roll), 1), "detection_score": round(face.score, 3), "flags": flags}

    def describe_all(self, img: np.ndarray) -> list[dict]:
        """Every face in the image (largest first) with quality measurements; no embeddings are computed."""
        faces, prepared, scale = self.detect_all(img)
        out = []
        for f in sorted(faces, key=lambda f: -f.area * f.score):
            with self._lock:
                aligned = self.recognizer.alignCrop(prepared, f.row)
            out.append(self.face_out(Analysis(len(faces), f, scale, None, self.quality(aligned, f, scale))))
        return out

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        with self._lock:
            return float(self.recognizer.match(a, b, cv2.FaceRecognizerSF_FR_COSINE))

    # ---- presentation helpers ----
    @staticmethod
    def face_out(an: Analysis) -> dict | None:
        if an.face is None:
            return None
        r, sc = an.face.row, an.scale
        pts = lambda i: [round(float(r[i]) / sc, 1), round(float(r[i + 1]) / sc, 1)]  # noqa: E731
        return {"box": [round(float(v) / sc, 1) for v in an.face.box],
                "landmarks": {"right_eye": pts(4), "left_eye": pts(6), "nose": pts(8), "right_mouth": pts(10), "left_mouth": pts(12)},
                "score": round(an.face.score, 3), "quality": an.quality}
