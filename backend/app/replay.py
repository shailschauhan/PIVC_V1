"""'What would change?' for a rule edit: re-decide past videos under proposed rules, without re-running any model.

Every analysed video keeps what each test measured (fake score, similarity, heart rate, ...). The tests turn those
measurements into a status with the rules, so the same measurements can be judged again under different rules.
Two rule sets are replayed (the live rules and the proposed ones) and compared with each other, so any small difference
between a replay and what was stored at the time cancels out. How often a replay under the live rules reproduces the
stored decision is reported as a check on the method.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone


from . import parameters
from .analyzers.checks import ANALYZERS
from .decision import decide
from .db import T, Db
from .rules import diff

SAMPLE_CAP = 20000  # newest videos examined; keeps the preview fast on very large histories
DECISIONS = ("Accept", "Needs Review", "Reject")


def _background(bg: dict | None, rules: dict) -> dict:
    """The background verdict depends on two thresholds, so it is re-derived from the stored measurements."""
    if not bg or not bg.get("assessed"):
        return bg or {"assessed": False, "virtual_suspected": False, "reasons": []}
    reasons = []
    if bg.get("blur_ratio") is not None and bg["blur_ratio"] < rules["bg_blur_ratio_max"]:
        reasons.append("blurred background")
    if bg.get("static_diff") is not None and bg["static_diff"] < rules["bg_static_diff_max"]:
        reasons.append("static / replaced background")
    return {**bg, "virtual_suspected": bool(reasons), "reasons": reasons}


def _obs(params: dict, rules: dict) -> dict:
    a = params["video_authenticity"]["details"]
    if a.get("error"):
        auth, score = {"error": a["error"]}, None
    else:
        per_frame = [f["score"] for f in a.get("frame_results", [])]
        auth = {"ensemble_score": a["fake_score"], "model_scores": a["model_scores"], "models": a["models"], "dataset": a["dataset"],
                "device": a["device"], "frames_sampled": a["frames_sampled"], "frames_with_face": a["frames_with_face"],
                "frames_without_face": a["frames_without_face"], "per_frame": per_frame, "frame_results": a.get("frame_results", []),
                "background": _background(a.get("background"), rules)}
        score = None if a["fake_score"] is None else 1 - a["fake_score"]
    get = lambda k: (params.get(k) or {}).get("details", {"error": "not run"})  # noqa: E731
    return {"authenticity_detail": auth, "authenticity_score": score, "vitals_detail": get("health_indicators"),
            "age_detail": get("age_estimation"), "face_detail": get("face_match"), "liveness_detail": get("liveness")}


def decide_again(report: dict, declared: dict, rules: dict) -> tuple[str, dict[str, str]] | None:
    """(decision, {test: status}) for the stored measurements under `rules`, or None if the record cannot be replayed."""
    from .analyzers.base import Context
    try:
        ctx = Context("replay", {}, rules, declared=declared or {})
        ctx.obs = _obs(report["parameters"], rules)
        params = {k: ANALYZERS[k](ctx) for k in ANALYZERS if k in report["parameters"] or k == "video_authenticity"}
        decision, _, _ = decide(params, rules)
        return decision, {k: p["status"] for k, p in params.items()}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def impact(db: Db, current: dict, proposed: dict, days: int = 30) -> dict:
    where, args = "status = 'ANALYZED' AND report IS NOT NULL", []
    if days:
        where, args = where + " AND created_at >= ?", [datetime.now(timezone.utc) - timedelta(days=days)]
    total_in_window = db.scalar(f"SELECT COUNT(*) FROM {T.cases} WHERE {where}", args)
    rows = ((r.id, r.case_ref, r.media, r.report, r.declared, r.decision, r.final_decision, r.review_status) for r in db.stream(
        f"SELECT TOP ({SAMPLE_CAP}) id, case_ref, media, report, declared, decision, final_decision, review_status FROM {T.cases} WHERE {where} ORDER BY id DESC",
        args, "cases", 300))

    dec = {"current": Counter(), "proposed": Counter()}
    matrix: Counter = Counter()
    tests = {p["key"]: {"current": Counter(), "proposed": Counter(), "changed": 0} for p in parameters.PARAMETERS}
    human = {"reviewed": 0, "current": Counter(), "proposed": Counter()}
    examples, replayed, reproduced, skipped = [], 0, 0, 0

    for cid, ref, media, report, declared, stored, final, review_status in rows:
        a, b = decide_again(report, declared, current), decide_again(report, declared, proposed)
        if a is None or b is None:
            skipped += 1
            continue
        replayed += 1
        reproduced += a[0] == stored
        dec["current"][a[0]] += 1
        dec["proposed"][b[0]] += 1
        for k, st in a[1].items():
            tests[k]["current"][st] += 1
        for k, st in b[1].items():
            tests[k]["proposed"][st] += 1
            tests[k]["changed"] += st != a[1].get(k)
        if a[0] != b[0]:
            matrix[(a[0], b[0])] += 1
            if len(examples) < 12:
                examples.append({"id": cid, "case_ref": ref, "filename": (media or {}).get("original_name") or ref,
                                 "from": a[0], "to": b[0], "final_decision": final})
        if review_status == "COMPLETED" and final in ("Accept", "Reject"):
            human["reviewed"] += 1
            for name, res in (("current", a[0]), ("proposed", b[0])):
                human[name]["agree" if res == final else "to_person" if res == "Needs Review" else "disagree"] += 1

    changed_keys = diff(current, proposed)
    return {
        "days": days, "in_window": total_in_window, "examined": replayed, "sampled": total_in_window > SAMPLE_CAP, "skipped": skipped,
        "replay_matches_stored": None if not replayed else reproduced / replayed,
        "changes": changed_keys,
        "decisions": {n: {d: dec[n][d] for d in DECISIONS} for n in dec},
        "flips": {"total": sum(matrix.values()), "matrix": [{"from": f, "to": t, "count": n} for (f, t), n in sorted(matrix.items(), key=lambda x: -x[1])]},
        "tests": [{"key": p["key"], "label": p["label"], "changed": tests[p["key"]]["changed"],
                   "current": dict(tests[p["key"]]["current"]), "proposed": dict(tests[p["key"]]["proposed"])}
                  for p in parameters.PARAMETERS if tests[p["key"]]["changed"]],
        "reviewers": {"reviewed": human["reviewed"],
                      **{n: {k: human[n][k] for k in ("agree", "disagree", "to_person")} for n in ("current", "proposed")}},
        "examples": examples,
    }
