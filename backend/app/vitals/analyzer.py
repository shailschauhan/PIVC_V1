"""Contactless vital signs from face video (remote photoplethysmography) using open-rppg.

open-rppg (https://github.com/KegangWangCCNU/open-rppg) estimates the blood-volume-pulse waveform from tiny
colour changes in the face, then derives heart rate, heart-rate variability and breathing rate. These are
*research-grade estimates*, not medical measurements: they degrade with video compression (the library itself
warns that videos with non-key frames damage the signal), motion, poor light and low resolution. Every result
therefore carries a signal-quality index (SQI) and the video-quality statistics, and the rules in
analyzers/checks.py refuse to call a reading reliable when quality is low.

The model is loaded once (lazily, ~25 s) and used under a lock: one analysis at a time.
"""
import logging
import os
import threading
from typing import Any

import numpy as np

log = logging.getLogger(__name__)
os.environ.setdefault("KERAS_BACKEND", "jax")  # must be set before keras is imported
MAX_BVP_POINTS = 300  # downsampled waveform kept for display
MIN_HRV_SECONDS = 20  # HRV needs enough beats; shorter clips only get HR and breathing-free output


def plausible_breathing(hz: float | None) -> float | None:
    """heartpy reports breathing in Hz and sometimes returns nonsense (0.0, >1 Hz); keep only 4-60 breaths/min."""
    if hz is None:
        return None
    bpm = hz * 60
    return round(bpm, 1) if 4 <= bpm <= 60 else None


class VitalsUnavailable(RuntimeError):
    pass


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (np.isnan(v) or np.isinf(v)) else v


class VitalsAnalyzer:
    def __init__(self, model_name: str = "FacePhys.rlap"):
        self.model_name = model_name
        self._lock = threading.Lock()
        self._model = None

    @staticmethod
    def importable() -> tuple[bool, str]:
        try:
            import importlib.util
            for mod in ("rppg", "jax", "keras", "heartpy", "av"):
                if importlib.util.find_spec(mod) is None:
                    return False, f"missing dependency: {mod}"
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def _load(self):
        if self._model is None:
            ok, why = self.importable()
            if not ok:
                raise VitalsUnavailable(f"Vitals dependencies not installed ({why})")
            try:
                import rppg
                self._model = rppg.Model(self.model_name)
            except Exception as e:  # noqa: BLE001
                raise VitalsUnavailable(f"Could not load rPPG model '{self.model_name}': {e}") from e
            log.info("open-rppg model %s loaded", self.model_name)

    def analyze(self, video_path: str) -> dict[str, Any]:
        """Raw measurements for one video. Raises VitalsUnavailable / ValueError on failure."""
        with self._lock:
            self._load()
            m = self._model
            try:
                m.process_video(video_path)
                stats = dict(m.statistic)
                bvp, ts = m.bvp()
                fps = float(m.fps)
                # The library computes HR, quality and HRV in one step and drops *all three* if the HRV step
                # fails (it does on short or noisy clips). Read HR + quality on their own, then try HRV separately.
                res = m.hr(return_hrv=False) or {}
                hrv_raw = {}
                dur = float(ts[-1] - ts[0]) if len(ts) > 1 else 0.0
                if len(bvp) and (_num(res.get("SQI")) or 0) > 0.5 and dur >= MIN_HRV_SECONDS:
                    try:
                        from rppg.main import get_prv
                        hrv_raw = get_prv(np.asarray(bvp), ts, fps)
                    except Exception as e:  # noqa: BLE001 - too few detectable beats, etc.
                        log.info("HRV not computed: %s", e)
            except Exception as e:  # noqa: BLE001 - decoding problems, no face, ...
                raise ValueError(f"rPPG analysis failed: {e}") from e
        hrv = {k: _num(v) for k, v in hrv_raw.items() if _num(v) is not None}
        duration = _num(ts[-1] - ts[0]) if len(ts) > 1 else 0.0
        wave = []
        if len(bvp) > 1:
            step = max(1, len(bvp) // MAX_BVP_POINTS)
            wave = [round(float(v), 3) for v in np.asarray(bvp)[::step]]
        frames = max(stats.get("frames", 0), stats.get("key", 0) + stats.get("dependent", 0))
        breathing = hrv.get("breathingrate")
        return {
            "model": self.model_name,
            "hr_bpm": _num(res.get("hr")),
            "sqi": _num(res.get("SQI")),
            "breathing_rate_bpm": plausible_breathing(breathing),
            "hrv": {
                "sdnn_ms": hrv.get("sdnn"), "rmssd_ms": hrv.get("rmssd"), "pnn50": hrv.get("pnn50"),
                "ibi_ms": hrv.get("ibi"), "lf_hf": hrv.get("LF/HF"), "bpm_peaks": hrv.get("bpm"),
            } if hrv else {},
            "duration_s": None if duration is None else round(duration, 1),
            "video": {
                "fps": round(fps, 1), "frames": frames, "key_frames": stats.get("key", 0),
                "non_key_frames": stats.get("dependent", 0), "no_face_frames": stats.get("null", 0),
                "non_key_fraction": round(stats.get("dependent", 0) / frames, 3) if frames else None,
            },
            "bvp": wave,
        }


_analyzer: VitalsAnalyzer | None = None


def get_analyzer() -> VitalsAnalyzer:
    global _analyzer
    if _analyzer is None:
        from ..config import settings
        _analyzer = VitalsAnalyzer(settings.vitals_model)
    return _analyzer


def engine_status() -> dict:
    from ..config import settings
    if not settings.vitals_enabled:
        return {"engine": "open-rppg", "ready": False, "reason": "disabled by configuration"}
    ok, why = VitalsAnalyzer.importable()
    return {"engine": "open-rppg", "ready": ok, "reason": why, "model": settings.vitals_model}
