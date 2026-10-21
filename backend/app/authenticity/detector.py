"""Deepfake detection on sampled video frames (DFSpot ensemble, from DeepFakeSol).

Pipeline: sample N evenly spaced frames -> BlazeFace face crops -> resize/normalize ->
each DFSpot model scores every face -> per-model video score = sigmoid(mean logit) ->
ensemble score = mean over models. Higher = more likely manipulated. Scores are NOT calibrated
probabilities; thresholds are business rules (see config.DEFAULT_RULES).
"""
import logging
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
FACE_SIZE = 224
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
MAX_FRAME_RESULTS = 64  # frame-level rows kept in the stored report


class DetectorUnavailable(RuntimeError):
    pass


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


def preprocess(face: np.ndarray) -> np.ndarray:
    """'scale' face policy of the original: pad small crops to 224, resize to 224x224, ImageNet-normalize."""
    h, w = face.shape[:2]
    if h < FACE_SIZE or w < FACE_SIZE:
        ph, pw = max(FACE_SIZE - h, 0), max(FACE_SIZE - w, 0)
        face = cv2.copyMakeBorder(face, ph // 2, ph - ph // 2, pw // 2, pw - pw // 2, cv2.BORDER_CONSTANT, value=0)
    face = cv2.resize(face, (FACE_SIZE, FACE_SIZE))
    face = (face.astype(np.float32) / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
    return face.transpose(2, 0, 1)


class DeepfakeDetector:
    def __init__(self, models_dir: str, dataset: str, model_names: list[str], frames_per_video: int):
        self.models_dir, self.dataset = Path(models_dir), dataset
        self.model_names, self.fpv = model_names, frames_per_video
        self._lock = threading.Lock()
        self._loaded = False

    def checkpoint(self, name: str) -> Path:
        from .models import REGISTRY
        return self.models_dir / f"{self.dataset}_{REGISTRY[name][1]}.pth"

    def missing(self) -> list[str]:
        try:
            return [str(self.checkpoint(n)) for n in self.model_names if not self.checkpoint(n).is_file()]
        except (KeyError, ImportError) as e:
            return [f"unusable model config ({e})"]

    def _load(self):
        with self._lock:
            if self._loaded:
                return
            try:
                import torch
                from .blazeface import BlazeFace, FaceExtractor, VideoReader
                from .models import REGISTRY
            except ImportError as e:  # torch/timm not installed
                raise DetectorUnavailable(f"Deepfake dependencies missing: {e}") from e
            if (m := self.missing()):
                raise DetectorUnavailable(f"Model checkpoint(s) not found: {', '.join(m)}")
            self.torch = torch
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            facedet = BlazeFace().to(self.device)
            facedet.load_weights(str(HERE / "blazeface" / "blazeface.pth"))
            facedet.load_anchors(str(HERE / "blazeface" / "anchors.npy"))
            reader = VideoReader(verbose=False)
            self.extractor = FaceExtractor(
                video_read_fn=lambda p: reader.read_frames(p, num_frames=self.fpv), facedet=facedet)
            self.nets = {}
            for name in self.model_names:
                net = REGISTRY[name][0]()
                # weights_only: refuse arbitrary pickled code inside checkpoints
                state = torch.load(self.checkpoint(name), map_location="cpu", weights_only=True)
                net.load_state_dict(state["net"] if "net" in state else state)
                self.nets[name] = net.eval().to(self.device)
            self._loaded = True
            log.info("Deepfake detector loaded: %s on %s", self.model_names, self.device)

    def analyze(self, video_path: str) -> dict[str, Any]:
        self._load()
        torch = self.torch
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        cap.release()

        frames = self.extractor.process_video(video_path)
        if not frames:
            raise ValueError("Video could not be decoded")
        with_face = [f for f in frames if len(f["faces"])]
        out: dict[str, Any] = {
            "dataset": self.dataset, "models": self.model_names, "device": str(self.device),
            "frames_requested": self.fpv, "frames_sampled": len(frames),
            "frames_with_face": len(with_face), "frames_without_face": len(frames) - len(with_face),
        }
        if not with_face:
            return {**out, "ensemble_score": None, "model_scores": {}, "frame_results": [], "per_frame": []}

        # raw material for the background heuristic (kept out of the stored report, only its result is stored)
        bg_items = [(f["frame"], tuple(float(v) for v in f["detections"][0][:4])) for f in with_face]
        out["_bg_items"] = bg_items

        batch = torch.from_numpy(np.stack([preprocess(f["faces"][0]) for f in with_face])).to(self.device)
        frame_scores: dict[str, np.ndarray] = {}
        model_scores: dict[str, float] = {}
        with torch.no_grad():
            for name, net in self.nets.items():
                logits = net(batch).cpu().numpy().flatten()
                frame_scores[name] = _sigmoid(logits)
                model_scores[name] = float(_sigmoid(logits.mean()))
        per_frame = np.mean(np.stack(list(frame_scores.values())), axis=0)
        return {
            **out,
            "ensemble_score": round(float(np.mean(list(model_scores.values()))), 4),
            "model_scores": {k: round(v, 4) for k, v in model_scores.items()},
            "per_frame": per_frame.tolist(),
            "frame_results": [
                {"frame": int(f["frame_idx"]), "seconds": round(f["frame_idx"] / fps, 2) if fps else None,
                 "score": round(float(s), 4),
                 "model_scores": {k: round(float(v[i]), 4) for k, v in frame_scores.items()}}
                for i, (f, s) in enumerate(zip(with_face, per_frame))
            ][:MAX_FRAME_RESULTS],
        }


_detector: DeepfakeDetector | None = None


def get_detector() -> DeepfakeDetector:
    global _detector
    if _detector is None:
        from ..config import settings
        _detector = DeepfakeDetector(settings.authenticity_models_dir, settings.authenticity_dataset,
                                     settings.authenticity_models, settings.authenticity_fpv)
    return _detector


def engine_status() -> dict:
    """Whether the real detector can run (health check, upload gate, dashboard banner)."""
    d = get_detector()
    try:
        import timm  # noqa: F401
        import torch  # noqa: F401
    except ImportError as e:
        return {"engine": "dfspot", "ready": False, "reason": f"missing dependency: {e}"}
    miss = d.missing()
    return {"engine": "dfspot", "ready": not miss, "models": d.model_names, "dataset": d.dataset,
            "reason": f"checkpoints not found: {', '.join(miss)}" if miss else "ok"}
