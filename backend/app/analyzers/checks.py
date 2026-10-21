"""Rule-based analyzers. Each turns model observations into a SOW-defined output."""
from .base import Context, result


def authenticity(ctx: Context) -> dict:
    """Maps the ensemble fake score to Pass / Review / Suspected Manipulation."""
    r, o = ctx.rules, ctx.obs
    det = o["authenticity_detail"]
    if det.get("error"):
        return result("Review", 0.0, "Video authenticity could not be assessed.", {"error": det["error"]},
                      [f"Authenticity analysis failed: {det['error']}"])
    if o["authenticity_score"] is None or det["frames_with_face"] < r["min_face_frames"]:
        return result("Review", 0.2, "Too few frames with a detectable face to assess authenticity.", _details(det, r),
                      [f"Only {det['frames_with_face']} of {det['frames_sampled']} sampled frames contain a face "
                       f"(minimum {r['min_face_frames']})"])
    fake = 1 - o["authenticity_score"]
    d = _details(det, r)
    # agreement between ensemble members scales confidence (1.0 for a single model)
    votes = [s >= r["fake_score_flag"] for s in det["model_scores"].values()]
    agree = 1.0 if len(votes) < 2 else max(votes.count(True), votes.count(False)) / len(votes)
    if fake < r["fake_score_review"]:
        return result("Pass", (1 - fake) * (0.8 + 0.2 * agree), "No signs of AI generation or manipulation.", d)
    if fake < r["fake_score_flag"]:
        return result("Review", 0.5, "Manipulation score is in the grey zone.", d,
                      [f"Authenticity inconclusive (fake score {fake:.2f})"])
    conf = (0.6 + 0.4 * min(1.0, (fake - r["fake_score_flag"]) / 0.4)) * (0.8 + 0.2 * agree)
    exc = [f"Suspected AI-generated/manipulated video (fake score {fake:.2f})"]
    bg = d["background"]
    if bg["virtual_suspected"] and r["virtual_bg_blocks_auto_reject"]:
        # segmentation halos are a known false-positive source, so a human must confirm this flag
        conf = min(conf, r["confidence_threshold"] - 0.01)
        exc.append(f"Possible {' / '.join(bg['reasons'])}: manipulation flag needs human confirmation")
    return result("Suspected Manipulation", conf, "Video shows signs of AI generation or manipulation.", d, exc)


def _details(det: dict, r: dict) -> dict:
    per = det.get("per_frame", [])
    return {
        "fake_score": det["ensemble_score"], "model_scores": det["model_scores"], "models": det["models"],
        "dataset": det["dataset"], "device": det["device"],
        "frames_sampled": det["frames_sampled"], "frames_with_face": det["frames_with_face"],
        "frames_without_face": det["frames_without_face"],
        "suspicious_frames": sum(1 for s in per if s >= r["fake_score_flag"]),
        "frame_results": det["frame_results"],
        "background": det.get("background") or {"assessed": False, "virtual_suspected": False, "reasons": []},
        "limitations": ["Scores are not calibrated probabilities.",
                        "A low score does not prove authenticity; a high score does not prove manipulation."],
    }


VITALS_LIMITATIONS = [
    "Contactless estimate from video (rPPG), not a medical measurement.",
    "Accuracy drops with compressed video, motion, poor lighting and low resolution.",
    "Health indicators are decision support only and are subject to human review.",
]


def vitals(ctx: Context) -> dict:
    """Heart rate / HRV / breathing from open-rppg -> Normal range | Flagged | Low signal quality | Unavailable.

    Readings are only trusted when the signal-quality index and duration allow it; otherwise the numbers are
    still shown for transparency but never flagged. Informational unless health_flag_requires_review is on.
    """
    r, d = ctx.rules, ctx.obs["vitals_detail"]
    if d.get("error"):
        return result("Unavailable", 0.0, "Vital signs could not be measured.", {"error": d["error"]})
    det = {**d, "limitations": VITALS_LIMITATIONS}
    sqi, hr, secs = d.get("sqi"), d.get("hr_bpm"), d.get("duration_s") or 0
    reasons = []
    if sqi is None or hr is None:
        reasons.append("no usable pulse signal (is a face visible?)")
    else:
        if sqi < r["health_sqi_min"]:
            reasons.append(f"signal quality {sqi:.2f} is below the minimum {r['health_sqi_min']}")
        if secs < r["health_min_seconds"]:
            reasons.append(f"video is {secs:.0f}s long; at least {r['health_min_seconds']}s is needed")
    nk = (d.get("video") or {}).get("non_key_fraction")
    if nk is not None and nk > 0.5 and reasons:
        reasons.append("compressed video (most frames are not key frames) weakens the pulse signal")
    det["reliable"], det["not_reliable_because"] = not reasons, reasons
    if reasons:
        return result("Low signal quality", sqi or 0.0, "Vital signs are not reliable for this video: " + "; ".join(reasons) + ".", det)

    flags = []
    if not r["hr_normal_low"] <= hr <= r["hr_normal_high"]:
        flags.append(f"Heart rate {hr:.0f} bpm outside {r['hr_normal_low']}-{r['hr_normal_high']} bpm")
    br = d.get("breathing_rate_bpm")
    if br is not None and not r["resp_normal_low"] <= br <= r["resp_normal_high"]:
        flags.append(f"Breathing rate {br:.0f}/min outside {r['resp_normal_low']}-{r['resp_normal_high']}/min")
    if flags:
        return result("Flagged", sqi, "Vital signs outside the configured range.", det, flags)
    return result("Normal range", sqi, f"Heart rate {hr:.0f} bpm" + (f", breathing {br:.0f}/min" if br is not None else "") + " within range.", det)


AGE_LIMITATIONS = [
    "Age is an estimate from the face (typical error is several years, larger on compressed or low-light video).",
    "Used for verification only; a mismatch with the declared age sends the video to human review, never to rejection.",
]


def declared_age(declared: dict) -> int | None:
    """Declared age in years from `declared_age` or `dob` (YYYY-MM-DD), else None."""
    from datetime import date
    try:
        if declared.get("declared_age") not in (None, ""):
            return int(float(declared["declared_age"]))
        if declared.get("dob"):
            b, t = date.fromisoformat(declared["dob"]), date.today()
            return t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    except (TypeError, ValueError):
        return None
    return None


def age_estimation(ctx: Context) -> dict:
    """Estimated age from MiVOLO -> Estimated age | Review (mismatch with declared age) | Low confidence | Unavailable."""
    r, d = ctx.rules, ctx.obs["age_detail"]
    if d.get("error"):
        return result("Unavailable", 0.0, "Age could not be estimated.", {"error": d["error"]})
    det = {**d, "limitations": AGE_LIMITATIONS}
    declared = declared_age(ctx.declared)
    det["declared_age"] = declared
    n, spread, age = d.get("frames_aged", 0), d.get("spread"), d.get("age")
    reasons = []
    if age is None or n < r["age_min_frames"]:
        reasons.append(f"only {n} frame(s) with a usable face (minimum {r['age_min_frames']})")
    elif spread is not None and spread > r["age_max_spread"]:
        reasons.append(f"per-frame estimates vary widely (spread {spread:.1f} years)")
    det["reliable"], det["not_reliable_because"] = not reasons, reasons
    # confidence in the estimate: tighter spread and more frames -> higher
    conf = 0.0 if age is None else max(0.2, min(0.95, 1 - (spread or 0) / 15)) * min(1.0, n / 10)
    if reasons:
        return result("Low confidence", conf, "Age estimate is not reliable: " + "; ".join(reasons) + ".", det)
    if declared is None:
        return result("Estimated age", conf, f"Estimated age {age:.0f} (range {d['age_low']:.0f}-{d['age_high']:.0f}); no declared age to compare.", det)
    diff = age - declared
    det["difference"] = round(diff, 1)
    if abs(diff) > r["age_tolerance_years"]:
        return result("Review", conf, f"Estimated age {age:.0f} differs from declared age {declared}.", det,
                      [f"Estimated age {age:.0f} vs declared {declared} ({diff:+.0f} years, tolerance {r['age_tolerance_years']})"])
    shown = round(diff)
    return result("Estimated age", conf, f"Estimated age {age:.0f} is consistent with declared age {declared} ({f'{shown:+d}' if shown else '0'} years).", det)


FACE_LIMITATIONS = [
    "Face similarity is uncalibrated: the thresholds must be set from real ID-photo / video pairs.",
    "A mismatch is never reported when image quality is poor; it becomes Review. A match is not proof of liveness.",
    "Used for verification only: a mismatch or doubt sends the video to human review, never to rejection.",
]
LIVENESS_LIMITATIONS = [
    "Passive, single-frame anti-spoofing: a risk signal, not proof. It can miss screen replays, masks and deepfakes.",
    "Spoof detection has not been measured on real attacks; close-up webcam frames are often not judgeable.",
    "Used for verification only: a suspected attack sends the video to human review, never to rejection.",
]


def face_match(ctx: Context) -> dict:
    """Match | Review | Mismatch | Not checked (no ID photo) | Unavailable, from the Face Match service's decision."""
    d = ctx.obs["face_detail"]
    if d.get("error"):
        return result("Unavailable", 0.0, "Face match could not be run.", {"error": d["error"]})
    if d.get("skipped"):
        return result("Not checked", 0.0, d["skipped"], {"skipped": d["skipped"]})
    r = ctx.rules
    det = {k: v for k, v in d.items() if k != "limitations"}
    det["limitations"] = FACE_LIMITATIONS
    sim, dec = d.get("similarity"), d["decision"]
    if dec == "match":
        conf = min(1.0, 0.5 + 0.5 * (sim - r["face_match_threshold"]) / max(1e-6, 1 - r["face_match_threshold"]))
        return result("Match", conf, f"Video face matches the ID document photo (similarity {sim:.2f}).", det)
    if dec == "mismatch":
        conf = min(1.0, 0.5 + 0.5 * (r["face_mismatch_threshold"] - sim) / max(1e-6, r["face_mismatch_threshold"]))
        return result("Mismatch", conf, f"Video face does not match the ID document photo (similarity {sim:.2f}).", det,
                      [f"Face does not match the ID document photo (similarity {sim:.2f}, mismatch below {r['face_mismatch_threshold']})"])
    why = d.get("reason", "")
    if dec == "no_face":
        return result("Review", 0.2, "No face could be compared.", det, [f"Face match not possible: {why}"])
    return result("Review", 0.4, "Face match is inconclusive.", det, [f"Face match inconclusive: {why}"])


def liveness(ctx: Context) -> dict:
    """Pass | Review (suspected attack or unclear) | Low confidence (not enough judgeable frames) | Unavailable."""
    d = ctx.obs["liveness_detail"]
    if d.get("error"):
        return result("Unavailable", 0.0, "Liveness could not be checked.", {"error": d["error"]})
    det = {k: v for k, v in d.items() if k != "limitations"}
    det["limitations"] = LIVENESS_LIMITATIONS
    p = d.get("real_probability")
    if d["decision"] == "no_face" or d.get("low_quality"):
        why = "no face found in the sampled frames" if d["decision"] == "no_face" else d.get("reason", "too few frames could be judged")
        return result("Low confidence", 0.0 if p is None else min(0.5, p), f"Liveness could not be judged reliably: {why}.", det)
    if d["decision"] == "pass":
        return result("Pass", p, f"Face looks like a real, present person (real-face probability {p:.2f}); this is a risk signal, not proof.", det)
    if d.get("spoof_suspected"):
        return result("Review", 1 - (p or 0), "Possible presentation attack (photo, screen or mask).", det,
                      [f"Liveness: possible presentation attack (real-face probability {p:.2f})"])
    return result("Review", 0.5, "Liveness is not clearly real.", det, [f"Liveness unclear (real-face probability {p:.2f})"])


ANALYZERS = {"video_authenticity": authenticity, "health_indicators": vitals, "age_estimation": age_estimation,
             "face_match": face_match, "liveness": liveness}
