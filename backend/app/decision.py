"""Overall decision: Accept / Reject / Needs Review. Anything uncertain goes to a human.

The decision is driven by video authenticity. Health indicators are informational (shown in the report) unless the
business rule `health_flag_requires_review` is switched on, in which case a Flagged reading turns an Accept into
Needs Review. Age estimation: a mismatch with the declared age
(status "Review") turns an Accept into Needs Review when `age_mismatch_requires_review` is on (default). It never causes
a Reject. Face match: a Mismatch or an inconclusive Review turns an Accept into Needs Review
(`face_mismatch_requires_review`, on by default). Liveness: a *suspected presentation attack* turns an Accept into Needs Review
(`liveness_suspected_requires_review`, on by default); unclear or unjudgeable liveness is reported only. Neither ever causes a
Reject. A health or age reading never causes a Reject, and an unreliable reading ("Low signal quality") never
changes the decision.
"""


def decide(params: dict[str, dict], rules: dict) -> tuple[str, float, list[str]]:
    exceptions = [f"[{k}] {e}" for k, p in params.items() for e in p["exceptions"]]
    p = params["video_authenticity"]
    confidence, sure = p["confidence"], p["confidence"] >= rules["confidence_threshold"]
    if p["status"] == "Suspected Manipulation" and sure:
        decision = "Reject"
    elif p["status"] == "Pass" and sure:
        decision = "Accept"
    else:  # Review, errors, too few faces, or a verdict below the confidence threshold
        decision = "Needs Review"
    health = params.get("health_indicators")
    if decision == "Accept" and rules.get("health_flag_requires_review") and health and health["status"] == "Flagged":
        decision = "Needs Review"
    age = params.get("age_estimation")
    if decision == "Accept" and rules.get("age_mismatch_requires_review") and age and age["status"] == "Review":
        decision = "Needs Review"
    face = params.get("face_match")
    if decision == "Accept" and rules.get("face_mismatch_requires_review") and face and face["status"] in ("Mismatch", "Review"):
        decision = "Needs Review"
    live = params.get("liveness")
    if (decision == "Accept" and rules.get("liveness_suspected_requires_review") and live and live["status"] == "Review"
            and live["details"].get("spoof_suspected")):
        decision = "Needs Review"
    return decision, round(confidence, 3), exceptions
