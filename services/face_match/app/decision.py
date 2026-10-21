"""Turning similarity scores into a decision. Pure functions (no models), so they are easy to test and to tune.

Similarity is the cosine similarity of two SFace embeddings (higher = more alike). OpenCV's reference threshold for
SFace is 0.363; ID-photo-versus-selfie pairs are harder than the benchmark, so the defaults here are more cautious and
there is a wide "review" band. All numbers are UNCALIBRATED until measured on real pairs.
"""
from statistics import median

import numpy as np

# Flags that make a face crop unreliable for proving a *mismatch*
SEVERE = {"too_small", "blurry", "off_angle"}


def decide_pair(similarity: float | None, match_t: float, mismatch_t: float, flags_a: list[str], flags_b: list[str]) -> dict:
    """One image against one image. A poor-quality image can lower similarity, so it can never prove a mismatch."""
    if similarity is None:
        return {"decision": "no_face", "reason": "No face could be found in one or both images."}
    poor = sorted(set(flags_a + flags_b) & SEVERE)
    if similarity >= match_t:
        return {"decision": "match", "reason": f"Similarity {similarity:.2f} is at or above the match threshold {match_t}."}
    if similarity < mismatch_t:
        if poor:
            return {"decision": "review", "reason": f"Similarity {similarity:.2f} is low, but image quality is poor ({', '.join(poor)}), so a mismatch is not conclusive."}
        return {"decision": "mismatch", "reason": f"Similarity {similarity:.2f} is below the mismatch threshold {mismatch_t}."}
    return {"decision": "review", "reason": f"Similarity {similarity:.2f} is between the mismatch ({mismatch_t}) and match ({match_t}) thresholds."}


def aggregate_frames(rows: list[dict], match_t: float, mismatch_t: float, ref_flags: list[str], min_usable: int) -> dict:
    """Video frames against one reference image -> one decision. Uses only good frames when there are enough of them."""
    scored = [r for r in rows if r.get("similarity") is not None]
    usable = [r for r in scored if not (set(r.get("quality_flags", [])) & SEVERE)]
    low_quality = len(usable) < min_usable
    pool = scored if low_quality else usable
    base = {"frames_sampled": len(rows), "frames_with_face": sum(1 for r in rows if r.get("face")),
            "frames_scored": len(scored), "frames_usable": len(usable), "low_quality": bool(scored) and low_quality}
    if not pool:
        return {**base, "decision": "no_face", "similarity": None, "reason": "No face could be compared (no face in the video frames, or none in the reference image)."}
    sims = np.array([r["similarity"] for r in pool], dtype=float)
    med, frac = float(median(sims)), float(np.mean(sims >= match_t))
    stats = {"similarity_median": round(med, 4), "similarity_max": round(float(sims.max()), 4), "similarity_min": round(float(sims.min()), 4),
             "similarity_p25": round(float(np.percentile(sims, 25)), 4), "fraction_frames_matching": round(frac, 3)}
    if med >= match_t and frac >= 0.5:
        d, why = "match", f"Median similarity {med:.2f} is at or above {match_t} and {frac:.0%} of frames match."
    elif med < mismatch_t and frac <= 0.1:
        poor = low_quality or bool(set(ref_flags) & SEVERE)
        if poor:
            d, why = "review", f"Median similarity {med:.2f} is low, but image/video quality is poor, so a mismatch is not conclusive."
        else:
            d, why = "mismatch", f"Median similarity {med:.2f} is below {mismatch_t} and only {frac:.0%} of frames match."
    else:
        d, why = "review", f"Median similarity {med:.2f} is not clearly a match or a mismatch ({frac:.0%} of frames match)."
    return {**base, **stats, "decision": d, "similarity": stats["similarity_median"], "reason": why}
