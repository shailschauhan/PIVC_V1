"""Business rules (thresholds). Every save is a new version: the table is append-only, so the history is the table."""
from datetime import datetime, timezone

from .config import DEFAULT_RULES
from .db import T, Db

UNIT_INTERVAL = ("confidence_threshold", "fake_score_review", "fake_score_flag", "bg_blur_ratio_max", "health_sqi_min",
                 "face_match_threshold", "face_mismatch_threshold", "liveness_pass_threshold", "liveness_spoof_threshold")
# (lower key, upper key): the lower one must stay below the upper one, or the middle band ("Review") disappears
ORDERED = (("fake_score_review", "fake_score_flag"), ("face_mismatch_threshold", "face_match_threshold"),
           ("liveness_spoof_threshold", "liveness_pass_threshold"), ("hr_normal_low", "hr_normal_high"),
           ("resp_normal_low", "resp_normal_high"))


class RuleError(ValueError):
    pass


def load_rules(db: Db) -> dict:
    row = db.one(f"SELECT TOP 1 * FROM {T.rule_config} ORDER BY id DESC", (), "rule_config")
    return {**DEFAULT_RULES, **(row.rules if row else {})}


def validate(changes: dict, base: dict) -> dict:
    """Returns the full rule set with `changes` applied, or raises RuleError with a message for the person editing."""
    unknown = sorted(set(changes) - set(DEFAULT_RULES))
    if unknown:
        raise RuleError(f"Unknown rule keys: {unknown}")
    merged = dict(base)
    for k, v in changes.items():
        want = DEFAULT_RULES[k]
        if isinstance(want, bool):
            if not isinstance(v, bool):
                raise RuleError(f"Invalid type for {k}")
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RuleError(f"Invalid type for {k}")
        elif v != v or v in (float("inf"), float("-inf")) or v < 0:
            raise RuleError(f"{k} must be a number that is not negative")
        merged[k] = v
    for k in UNIT_INTERVAL:
        if not 0 <= merged[k] <= 1:
            raise RuleError(f"{k} must be between 0 and 1")
    for lo, hi in ORDERED:
        if merged[lo] >= merged[hi]:
            raise RuleError(f"{lo} must be lower than {hi}")
    if merged["review_sla_hours"] <= 0:
        raise RuleError("review_sla_hours must be more than 0")
    return merged


def diff(old: dict, new: dict) -> list[dict]:
    return [{"key": k, "from": old.get(k), "to": new.get(k)} for k in DEFAULT_RULES if old.get(k) != new.get(k)]


def save_rules(db: Db, rules: dict, who: str | None = None, note: str | None = None) -> dict:
    """Validates and appends a version (the caller commits nothing else in between). Returns the new rule set."""
    merged = validate({k: v for k, v in rules.items()}, load_rules(db))
    db.insert("rule_config", rules=merged, updated_at=datetime.now(timezone.utc), updated_by=who, note=(note or "").strip()[:300] or None)
    db.commit()
    return merged


def history(db: Db, limit: int = 200) -> list[dict]:
    """Newest first. Each entry lists what it changed compared with the version before it (the first compares with the defaults)."""
    rows = db.all(f"SELECT TOP (?) * FROM {T.rule_config} ORDER BY id DESC", (limit + 1,), "rule_config")
    out = []
    for i, r in enumerate(rows[:limit]):
        before = {**DEFAULT_RULES, **rows[i + 1].rules} if i + 1 < len(rows) else DEFAULT_RULES
        full = {**DEFAULT_RULES, **r.rules}
        out.append({"version": r.id, "at": r.updated_at, "by": r.updated_by, "note": r.note,
                    "changes": diff(before, full), "current": i == 0})
    return out


def version_rules(db: Db, version: int) -> dict | None:
    row = db.get("rule_config", version)
    return {**DEFAULT_RULES, **row.rules} if row else None
