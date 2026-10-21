"""Passive face anti-spoofing: YuNet finds the face, two MiniFASNet models (Minivision, Apache-2.0) score "real vs spoof".

Re-implements the preprocessing of Silent-Face-Anti-Spoofing's test.py exactly: for each model, crop the face box expanded by that
model's scale (2.7 and 4.0), resize to 80x80, feed raw BGR values 0-255 (no mean/std), softmax over 3 classes where class 1 is
"real", and sum the two models' outputs. Upstream finds the face with a RetinaFace caffemodel; we use YuNet instead, whose boxes
are similar but not identical, so measure on your own footage.
"""
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from .config import Settings

YUNET = "face_detection_yunet_2023mar.onnx"
INPUT = 80
DETECT_MAX_SIDE = 960   # detection runs on a copy no larger than this; boxes are mapped back to the original frame


class ModelsMissing(RuntimeError):
    pass


@dataclass
class Detected:
    box: tuple[int, int, int, int]   # x, y, w, h in the original frame
    score: float


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def new_box(src_w: int, src_h: int, bbox: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int, float]:
    """Port of upstream CropImage._get_new_box. Also returns the scale actually used (it shrinks near the frame border)."""
    x, y, bw, bh = bbox
    eff = min((src_h - 1) / bh, min((src_w - 1) / bw, scale))
    nw, nh = bw * eff, bh * eff
    cx, cy = bw / 2 + x, bh / 2 + y
    lx, ly, rx, ry = cx - nw / 2, cy - nh / 2, cx + nw / 2, cy + nh / 2
    if lx < 0:
        rx -= lx
        lx = 0
    if ly < 0:
        ry -= ly
        ly = 0
    if rx > src_w - 1:
        lx -= rx - src_w + 1
        rx = src_w - 1
    if ry > src_h - 1:
        ly -= ry - src_h + 1
        ry = src_h - 1
    return int(lx), int(ly), int(rx), int(ry), eff


def crop(img: np.ndarray, bbox: tuple[int, int, int, int], scale: float) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    lx, ly, rx, ry, eff = new_box(w, h, bbox, scale)
    return cv2.resize(img[ly:ry + 1, lx:rx + 1], (INPUT, INPUT)), eff


class LivenessEngine:
    def __init__(self, s: Settings):
        self.s = s
        yunet = s.models_dir / YUNET
        onnx = sorted(s.models_dir.glob("*_80x80_MiniFASNet*.onnx"))
        missing = ([] if yunet.is_file() else [str(yunet)]) + ([] if onnx else [str(s.models_dir / "*_80x80_MiniFASNet*.onnx")])
        if missing:
            raise ModelsMissing(f"Model file(s) not found: {', '.join(missing)}. Run: python scripts/download_models.py")
        self.detector = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320), s.det_score_threshold, 0.3, 50)
        self.models: list[tuple[float, str, ort.InferenceSession]] = []
        for p in onnx:
            scale = float(p.name.split("_")[0])  # upstream naming: '<scale>_80x80_<arch>' (4_0_0_... means 4.0)
            self.models.append((scale, p.name, ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])))
        self._lock = threading.Lock()

    def info(self) -> dict:
        return {name: {"scale": sc} for sc, name, _ in self.models}

    def detect(self, img: np.ndarray) -> list[Detected]:
        h, w = img.shape[:2]
        k = min(1.0, DETECT_MAX_SIDE / max(h, w))
        small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1.0 else img
        with self._lock:
            self.detector.setInputSize((small.shape[1], small.shape[0]))
            _, rows = self.detector.detect(small)
        out = []
        for r in (rows if rows is not None else []):
            x, y, bw, bh = (float(v) / k for v in r[:4])
            x, y = max(0, int(x)), max(0, int(y))
            out.append(Detected((x, y, max(1, min(int(bw), w - x)), max(1, min(int(bh), h - y))), float(r[14])))
        return out

    def score(self, img: np.ndarray, box: tuple[int, int, int, int]) -> dict:
        probs, eff = [], []
        batch_per_model = []
        for scale, name, sess in self.models:
            patch, used = crop(img, box, scale)
            eff.append((scale, used))
            batch_per_model.append((scale, name, sess, patch.astype(np.float32).transpose(2, 0, 1)[None]))
        with self._lock:
            for scale, name, sess, x in batch_per_model:
                p = softmax(sess.run(None, {"input": x})[0])[0]
                probs.append({"model": name, "scale": scale, "probs": [round(float(v), 4) for v in p]})
        total = sum(np.array(m["probs"]) for m in probs)
        real = float(total[1] / len(probs))
        flags = []
        w, h = box[2], box[3]
        if min(w, h) < self.s.min_face_px:
            flags.append("too_small")
        if any(used < 0.85 * sc for sc, used in eff):
            flags.append("tight_framing")       # not enough surrounding context for the model's crop (e.g. an ID-photo crop)
        return {"real_probability": round(real, 4), "label": "real" if int(np.argmax(total)) == 1 else "spoof",
                "models": probs, "flags": flags}

    def analyze(self, img: np.ndarray) -> dict:
        faces = self.detect(img)
        if not faces:
            return {"faces_found": 0, "face": None}
        main = max(faces, key=lambda f: f.box[2] * f.box[3] * f.score)
        return {"faces_found": len(faces), "face": {"box": list(main.box), "detection_score": round(main.score, 3), **self.score(img, main.box)}}
