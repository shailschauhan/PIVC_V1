"""Turning per-frame "real" probabilities into one decision. Pure functions (no models): easy to test and to tune.

The decision is deliberately asymmetric: a convincing pass is "pass", everything else is "review". The service never says
"this is a spoof" as a verdict, only `spoof_suspected: true` when the evidence is consistently strong, because a passive
single-frame model on compressed video is a risk signal, not proof. Thresholds are UNCALIBRATED.
"""
from statistics import median

import numpy as np

UNRELIABLE = {"too_small", "tight_framing"}   # frames whose crop the models cannot judge well


def aggregate(rows: list[dict], pass_t: float, spoof_t: float, min_usable: int) -> dict:
    scored = [r for r in rows if r.get("real_probability") is not None]
    usable = [r for r in scored if not (set(r.get("flags", [])) & UNRELIABLE)]
    low_quality = len(usable) < min_usable
    pool = scored if low_quality else usable
    base = {"frames_sampled": len(rows), "frames_with_face": sum(1 for r in rows if r.get("face")), "frames_scored": len(scored),
            "frames_usable": len(usable), "low_quality": bool(scored) and low_quality}
    if not pool:
        return {**base, "decision": "no_face", "spoof_suspected": False, "real_probability": None,
                "reason": "No face could be assessed (no face found in the sampled frames)."}
    p = np.array([r["real_probability"] for r in pool], dtype=float)
    med, frac = float(median(p)), float(np.mean(p >= 0.5))
    stats = {"real_probability_median": round(med, 4), "real_probability_p10": round(float(np.percentile(p, 10)), 4),
             "real_probability_min": round(float(p.min()), 4), "fraction_frames_real": round(frac, 3)}
    suspected = med < spoof_t and frac <= 0.2 and not low_quality
    if med >= pass_t and frac >= 0.6 and not low_quality:
        d, why = "pass", f"Median real-face probability {med:.2f} is at or above {pass_t} and {frac:.0%} of frames look real."
    elif suspected:
        d, why = "review", f"Median real-face probability {med:.2f} is below {spoof_t} and only {frac:.0%} of frames look real: possible presentation attack."
    elif low_quality:
        d, why = "review", f"Too few good frames ({len(usable)} usable, need {min_usable}) to judge liveness reliably."
    else:
        d, why = "review", f"Median real-face probability {med:.2f} is not clearly real ({frac:.0%} of frames look real)."
    return {**base, **stats, "decision": d, "spoof_suspected": suspected, "real_probability": stats["real_probability_median"], "reason": why}
