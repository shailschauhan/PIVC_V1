"""Heuristic detection of blurred / replaced ("Teams-style") backgrounds.

Why it matters: segmentation masks leave soft halos around hair and shoulders, and a replaced background
removes real-world context. Neither is evidence of a deepfake, but both are out-of-distribution for the
DFSpot models. We therefore *measure* it, show it to reviewers, and never auto-reject on a manipulation flag
when it is present (see checks.authenticity). It is deliberately not used to auto-accept or to raise a flag.

Signals, taken from the side strips beside the person (face box widened to head+shoulders):
  blur_ratio : Laplacian sharpness of the background / sharpness of the face   (blur -> small)
  static_diff: mean |frame_t - frame_t-1| over the same strips across sampled frames
               (a pasted still image is almost perfectly constant, a real room has sensor noise)
Thresholds were set from a handful of real clips plus synthetic variants of them: a pasted still image is
detected reliably (frame diff 0.00-0.15 vs 0.56-1.2 for real footage); blur is only partly detected (2 of 3
variants; noisy low-resolution footage stays "sharp"). Validate on real Teams/Zoom/Meet recordings. When the
face fills the frame there is no background to judge and the result is "not assessed".
"""
import cv2
import numpy as np

SIDE_FACTOR = 1.2   # how far beside the face the "person" zone extends, in face widths
MIN_STRIP_PX = 24   # strips narrower than this are not assessed
MIN_FRAMES = 4


def _gray(frame: np.ndarray) -> np.ndarray:
    return frame.mean(axis=2).astype(np.float32) if frame.ndim == 3 else frame.astype(np.float32)


def _lap_var(img: np.ndarray) -> float:
    return float(cv2.Laplacian(img, cv2.CV_32F).var())


def analyze_background(items: list[tuple[np.ndarray, tuple[float, float, float, float]]], rules: dict) -> dict:
    """items: [(frame HxWx3, (ymin, xmin, ymax, xmax) face box)] for the sampled frames, in time order."""
    strips, sharp_bg, sharp_face = [], [], []
    for frame, (ymin, xmin, ymax, xmax) in items:
        h, w = frame.shape[:2]
        fw = xmax - xmin
        x0, x1 = int(max(0, xmin - SIDE_FACTOR * fw)), int(min(w, xmax + SIDE_FACTOR * fw))
        if x0 < MIN_STRIP_PX and (w - x1) < MIN_STRIP_PX:
            continue  # person fills the frame; nothing to look at
        g = _gray(frame)
        side = [g[:, :x0]] if x0 >= MIN_STRIP_PX else []
        side += [g[:, x1:]] if (w - x1) >= MIN_STRIP_PX else []
        face = g[max(0, int(ymin)):int(ymax), max(0, int(xmin)):int(xmax)]
        if face.size == 0:
            continue
        sharp_bg.append(np.mean([_lap_var(s) for s in side]))
        sharp_face.append(_lap_var(face))
        strips.append((g, x0, x1))
    if len(strips) < MIN_FRAMES:
        return {"assessed": False, "virtual_suspected": False, "reasons": [], "note": "background not visible enough to assess"}

    blur_ratio = float(np.median(sharp_bg) / max(np.median(sharp_face), 1e-6))
    # compare only columns that are background in every frame
    lo, hi = min(s[1] for s in strips), max(s[2] for s in strips)
    diffs = []
    for (a, _, _), (b, _, _) in zip(strips, strips[1:]):
        parts = [np.abs(a[:, :lo] - b[:, :lo])] if lo >= MIN_STRIP_PX else []
        parts += [np.abs(a[:, hi:] - b[:, hi:])] if (a.shape[1] - hi) >= MIN_STRIP_PX else []
        if parts:
            diffs.append(float(np.mean([p.mean() for p in parts])))
    static_diff = float(np.median(diffs)) if diffs else None

    reasons = []
    if blur_ratio < rules["bg_blur_ratio_max"]:
        reasons.append("blurred background")
    if static_diff is not None and static_diff < rules["bg_static_diff_max"]:
        reasons.append("static / replaced background")
    return {
        "assessed": True, "virtual_suspected": bool(reasons), "reasons": reasons,
        "blur_ratio": round(blur_ratio, 3), "static_diff": None if static_diff is None else round(static_diff, 3),
        "frames_assessed": len(strips),
    }
