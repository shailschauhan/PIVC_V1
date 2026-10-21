"""Age-estimation worker (MiVOLO v2). Runs in its own virtualenv; the main app talks to it over stdin/stdout.

Protocol: one JSON object per line.
  <- {"id": 1, "video": "path.mp4", "frames": 16}
  -> {"id": 1, "ok": true, "result": {...}}  |  {"id": 1, "ok": false, "error": "..."}
It prints {"ready": true} once the models are loaded. Everything the libraries print goes to stderr.

Per sampled frame: YOLOv8 finds faces and bodies -> MiVOLO estimates age for each face (using the matched body
crop when there is one) -> the largest face is taken as the subject. The main app aggregates over frames.
"""
import json
import os
import sys
import traceback
from pathlib import Path

_proto = sys.stdout            # keep the real stdout for the protocol only
sys.stdout = sys.stderr        # anything libraries print goes to stderr

sys.path.insert(0, str(Path(__file__).resolve().parent))  # vendored `mivolo` package
os.environ.setdefault("YOLO_VERBOSE", "False")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402


def send(obj) -> None:
    _proto.write(json.dumps(obj) + "\n")
    _proto.flush()


class AgeEstimator:
    def __init__(self, models_dir: Path, device: str = "cpu"):
        from mivolo.model.yolo_detector import Detector
        from transformers import AutoImageProcessor, AutoModelForImageClassification

        self.device = torch.device(device)
        self.detector = Detector(str(models_dir / "yolov8x_person_face.pt"), device, half=False, verbose=False)
        mdir = str(models_dir / "mivolo_v2")
        self.model = AutoModelForImageClassification.from_pretrained(mdir, trust_remote_code=True, torch_dtype=torch.float32)
        self.model = self.model.to(self.device).eval()
        self.processor = AutoImageProcessor.from_pretrained(mdir, trust_remote_code=True)

    @torch.no_grad()
    def frame(self, bgr: np.ndarray) -> dict:
        det = self.detector.predict(bgr)
        h, w = bgr.shape[:2]
        out = {"n_faces": det.n_faces, "n_persons": det.n_persons, "subject": None}
        if det.n_faces == 0:
            return out
        det.associate_faces_with_persons()
        crops = det.collect_crops(bgr)
        (b_inds, b_crops), (f_inds, f_crops) = crops.get_faces_with_bodies(True, True)
        if not any(c is not None for c in f_crops):
            return out
        faces_in = self.processor(images=f_crops)["pixel_values"].to(self.device, torch.float32)
        body_in = self.processor(images=b_crops)["pixel_values"].to(self.device, torch.float32)
        ages = self.model(faces_input=faces_in, body_input=body_in).age_output.flatten().tolist()
        best = None
        for i, ind in enumerate(f_inds):
            if ind is None or f_crops[i] is None:
                continue
            x1, y1, x2, y2 = [float(v) for v in det.get_bbox_by_ind(ind, h, w)]
            area = max(0.0, (x2 - x1) * (y2 - y1)) / float(h * w)
            if best is None or area > best["face_area_frac"]:
                best = {"age": float(ages[i]), "face_area_frac": round(area, 4),
                        "face_conf": round(float(det.yolo_results.boxes.conf[ind]), 3), "with_body": b_crops[i] is not None}
        out["subject"] = best
        return out


def sample_indices(n_total: int, n: int) -> list[int]:
    if n_total <= 0:
        return []
    return sorted({int(round(i)) for i in np.linspace(0, n_total - 1, min(n, n_total))})


def analyze(est: AgeEstimator, video: str, n_frames: int) -> dict:
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise ValueError("Video could not be opened")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    rows = []
    for idx in sample_indices(total, n_frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        r = est.frame(frame)
        rows.append({"frame": idx, "seconds": round(idx / fps, 2) if fps else None, "n_faces": r["n_faces"],
                     **({"age": round(r["subject"]["age"], 1), "face_conf": r["subject"]["face_conf"],
                         "face_area_frac": r["subject"]["face_area_frac"], "with_body": r["subject"]["with_body"]}
                        if r["subject"] else {})})
    cap.release()
    if not rows:
        raise ValueError("Video could not be decoded")
    return {"frames_sampled": len(rows), "frames": rows}


def main() -> None:
    models_dir = Path(sys.argv[1])
    est = AgeEstimator(models_dir, sys.argv[2] if len(sys.argv) > 2 else "cpu")
    send({"ready": True})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        try:
            send({"id": req["id"], "ok": True, "result": analyze(est, req["video"], int(req.get("frames", 16)))})
        except Exception as e:  # noqa: BLE001 - report, keep serving
            traceback.print_exc()
            send({"id": req.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
