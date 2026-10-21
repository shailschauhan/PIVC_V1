"""Dashboard numbers. SQL aggregates only (GROUP BY), so it stays fast with hundreds of thousands of videos.

Everything is computed for a window ("the last N days") and, so that every figure can show a trend, for the equally long
window just before it. `days=1` gives hourly buckets, `days=0` means all time.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from statistics import median

from . import parameters
from .db import T, Db
from .rules import load_rules

DECISIONS = ("Accept", "Needs Review", "Reject")
MAX_DAILY_POINTS = 366


def _window(since: datetime | None, until: datetime | None, col: str = "created_at") -> tuple[str, list]:
    """' AND created_at >= ? AND created_at < ?' for whichever bounds are given, and their values."""
    sql, args = "", []
    if since:
        sql, args = sql + f" AND {col} >= ?", args + [since]
    if until:
        sql, args = sql + f" AND {col} < ?", args + [until]
    return sql, args


def _nth(db: Db, sql: str, args: list, order: str, n: int, count: int = 1) -> list:
    """`count` values starting at position n (0-based) of the sorted result, without loading the rest."""
    return [r["v"] for r in db.all(f"{sql} ORDER BY {order} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", [*args, n, count])]


def kpis(db: Db, since: datetime | None, until: datetime | None) -> dict:
    wsql, wargs = _window(since, until)
    # one pass over the window; every count below is derived from these groups
    groups = db.all(
        f"SELECT status, decision, review_status, COUNT(*) AS n, "
        f"SUM(CASE WHEN review_status = 'COMPLETED' AND decision <> 'Needs Review' AND final_decision <> decision THEN 1 ELSE 0 END) AS overturned, "
        f"SUM(CASE WHEN status = 'ANALYZED' THEN confidence END) AS conf_sum, "
        f"SUM(CASE WHEN status = 'ANALYZED' AND confidence IS NOT NULL THEN 1 ELSE 0 END) AS conf_n, "
        f"SUM(CASE WHEN status = 'ANALYZED' AND processing_ms IS NOT NULL THEN 1 ELSE 0 END) AS time_n "
        f"FROM {T.cases} WHERE 1 = 1{wsql} GROUP BY status, decision, review_status", wargs)
    by_status: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    reviewed = queue_reviewed = overturned = time_n = conf_n = 0
    conf_sum = 0.0
    for g in groups:
        by_status[g.status] = by_status.get(g.status, 0) + g.n
        if g.status == "ANALYZED":
            by_decision[g.decision] = by_decision.get(g.decision, 0) + g.n
        if g.review_status == "COMPLETED":
            reviewed += g.n
            queue_reviewed += g.n if g.decision == "Needs Review" else 0
        overturned += g.overturned or 0
        conf_sum += g.conf_sum or 0.0
        conf_n += g.conf_n or 0
        time_n += g.time_n or 0
    analyzed = sum(by_decision.values())
    needs = by_decision.get("Needs Review", 0)
    avg_conf = conf_sum / conf_n if conf_n else None

    times_sql = f"SELECT processing_ms AS v FROM {T.cases} WHERE status = 'ANALYZED' AND processing_ms IS NOT NULL{wsql}"
    p50 = p95 = None
    if time_n:  # the value at the same position the percentile has always used: sorted[min(n - 1, int(q * n))]
        p50 = _nth(db, times_sql, wargs, "processing_ms", min(time_n - 1, int(0.5 * time_n)))[0]
        p95 = _nth(db, times_sql, wargs, "processing_ms", min(time_n - 1, int(0.95 * time_n)))[0]

    waited = (f"FROM {T.cases} WHERE review_status = 'COMPLETED' AND decision = 'Needs Review' "
              f"AND analyzed_at IS NOT NULL AND reviewed_at IS NOT NULL{wsql}")
    wn = db.scalar(f"SELECT COUNT(*) {waited}", wargs)
    med_h = None
    if wn:  # median of the hours between "sent to review" and "decided": the middle one or the two middle ones
        mid = _nth(db, f"SELECT DATEDIFF_BIG(millisecond, analyzed_at, reviewed_at) AS v {waited}", wargs, "v", (wn - 1) // 2, 1 if wn % 2 else 2)
        med_h = round(median(mid) / 3_600_000, 2)

    auto = analyzed - needs
    return {
        "total": sum(by_status.values()), "analyzed": analyzed, "failed": by_status.get("FAILED", 0),
        "queue": by_status.get("PENDING", 0) + by_status.get("PROCESSING", 0),
        "accept": by_decision.get("Accept", 0), "reject": by_decision.get("Reject", 0), "needs_review": needs,
        "auto_decided": auto, "reviewed": reviewed, "queue_reviewed": queue_reviewed, "overturned": overturned,
        "straight_through_rate": round(auto / analyzed, 4) if analyzed else None,
        "overturn_rate": round(overturned / auto, 4) if auto else None,
        "avg_confidence": round(avg_conf, 4) if avg_conf is not None else None,
        "p50_ms": p50, "p95_ms": p95,
        "median_review_hours": med_h,
    }


def tests(db: Db, since: datetime | None, until: datetime | None, rules: dict, with_distribution: bool = True) -> list[dict]:
    """One entry per registered test. `clean` / `attention` / `unassessed` come from the tone of each outcome
    (good / warn+bad / muted), so every test reads the same way: how many were fine, how many need a person,
    and how many the test could not assess."""
    wsql, wargs = _window(since, until, "c.created_at")
    windowed = bool(since or until)
    frm = f"{T.case_params} p" + (f" JOIN {T.cases} c ON c.id = p.case_id" if windowed else "")
    where = f"WHERE 1 = 1{wsql}" if windowed else ""
    counts: dict[str, dict[str, int]] = {}
    for r in db.all(f"SELECT p.parameter AS k, p.status AS st, COUNT(*) AS n FROM {frm} {where} GROUP BY p.parameter, p.status", wargs):
        counts.setdefault(r.k, {})[r.st] = r.n
    dists: dict[str, list[int]] = {}
    if with_distribution:
        parts, args = [], []  # every test's histogram in one round trip
        for spec in parameters.PARAMETERS:
            d = spec["distribution"]
            bucket = f"CAST((p.score - {float(d['lo'])!r}) / {float(d['hi'] - d['lo'])!r} * {int(d['bins'])} AS INT)"
            parts.append(f"SELECT p.parameter AS k, {bucket} AS b, COUNT(*) AS n FROM {frm} WHERE p.parameter = ? AND p.score IS NOT NULL{wsql} GROUP BY p.parameter, {bucket}")
            args += [spec["key"], *wargs]
        for r in db.all(" UNION ALL ".join(parts), args):
            nb = parameters.BY_KEY[r.k]["distribution"]["bins"]
            bins = dists.setdefault(r.k, [0] * nb)
            bins[min(nb - 1, max(0, r.b))] += r.n
    out = []
    for spec in parameters.PARAMETERS:
        got = dict(counts.get(spec["key"], {}))
        total = sum(got.values())
        outcomes = [{"status": s, "tone": tone, "count": got.pop(s, 0)} for s, tone in spec["outcomes"]]
        outcomes += [{"status": s, "tone": "muted", "count": n} for s, n in got.items()]
        by_tone = {t: sum(o["count"] for o in outcomes if o["tone"] in group) for t, group in
                   (("clean", ("good",)), ("attention", ("warn", "bad")), ("unassessed", ("muted",)))}
        item = {
            "key": spec["key"], "label": spec["label"], "engine": spec["engine"], "question": spec["question"],
            "total": total, "outcomes": outcomes, **by_tone,
            **{f"{t}_rate": (round(by_tone[t] / total, 4) if total else None) for t in by_tone},
        }
        if with_distribution:
            dist = spec["distribution"]
            span = dist["hi"] - dist["lo"]
            item["distribution"] = {
                "label": dist["label"], "unit": dist["unit"], "lo": dist["lo"], "hi": dist["hi"], "bins": dists.get(spec["key"], [0] * dist["bins"]),
                "markers": [{"at": (rules[r] - dist["lo"]) / span, "label": lbl, "value": rules[r]} for r, lbl in dist["markers"] if r in rules]}
        out.append(item)
    return out


def trend(db: Db, days: int, since: datetime | None, now: datetime) -> tuple[str, list[dict]]:
    """Analyzed videos per bucket by decision, with empty buckets filled in so the time axis is continuous."""
    wsql, wargs = _window(since, None)
    if days == 1:  # hourly: small enough to bucket in Python
        start = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
        buckets = {start + timedelta(hours=i): {d: 0 for d in DECISIONS} for i in range(24)}
        for r in db.all(f"SELECT created_at, decision FROM {T.cases} WHERE status = 'ANALYZED'{wsql}", wargs):
            key = r.created_at.replace(minute=0, second=0, microsecond=0)
            if key in buckets and r.decision in DECISIONS:
                buckets[key][r.decision] += 1
        return "hour", [{"t": k.isoformat(), **v} for k, v in buckets.items()]
    rows: dict[str, dict[str, int]] = {}
    for r in db.all(f"SELECT CONVERT(date, created_at) AS d, decision, COUNT(*) AS n FROM {T.cases} WHERE status = 'ANALYZED'{wsql} "
                    f"GROUP BY CONVERT(date, created_at), decision", wargs):
        rows.setdefault(str(r.d), {})[r.decision] = r.n
    first = (since or now - timedelta(days=MAX_DAILY_POINTS - 1)).date()
    if not since and rows:
        first = max(min(datetime.fromisoformat(k).date() for k in rows), (now - timedelta(days=MAX_DAILY_POINTS - 1)).date())
    out, cur = [], first
    while cur <= now.date():
        r = rows.get(cur.isoformat(), {})
        out.append({"t": cur.isoformat(), **{d: r.get(d, 0) for d in DECISIONS}})
        cur += timedelta(days=1)
    return "day", out


def engines() -> list[dict]:
    """Engine health, probed in parallel (each may be a network call to a service)."""
    with ThreadPoolExecutor(max_workers=len(parameters.PARAMETERS)) as pool:
        states = list(pool.map(lambda p: parameters.engine_status_fn(p)(), parameters.PARAMETERS))
    return [{"key": p["key"], "label": p["label"], **st} for p, st in zip(parameters.PARAMETERS, states)]


def build(db: Db, days: int) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None
    rules = load_rules(db)
    sla = float(rules.get("review_sla_hours", 24))

    cur = kpis(db, since, None)
    prev = None
    if since:
        span = now - since
        p_since, p_until = since - span, since
        prev = {"kpis": kpis(db, p_since, p_until),
                "tests": {t["key"]: {"attention_rate": t["attention_rate"], "clean_rate": t["clean_rate"], "unassessed_rate": t["unassessed_rate"], "total": t["total"]}
                          for t in tests(db, p_since, p_until, rules, with_distribution=False)}}

    # the backlog is "now", never windowed
    pend = db.one(f"SELECT COUNT(*) AS n, SUM(CASE WHEN analyzed_at < ? THEN 1 ELSE 0 END) AS over_sla FROM {T.cases} WHERE review_status = 'PENDING'",
                  (now - timedelta(hours=sla),))
    oldest = db.all(f"SELECT TOP 8 id, case_ref, media, confidence, analyzed_at FROM {T.cases} WHERE review_status = 'PENDING' ORDER BY analyzed_at ASC", (), "cases")

    wsql, wargs = _window(since, None)
    bins = [0] * 10
    for r in db.all(f"SELECT CAST(confidence * 10 AS INT) AS b, COUNT(*) AS n FROM {T.cases} WHERE status = 'ANALYZED' AND confidence IS NOT NULL{wsql} "
                    f"GROUP BY CAST(confidence * 10 AS INT)", wargs):
        bins[min(9, max(0, r.b))] += r.n
    esql, eargs = _window(since, None, "c.created_at")
    join = f"JOIN {T.cases} c ON c.id = e.case_id WHERE 1 = 1{esql}" if since else ""
    reasons = [{"parameter": r.parameter, "reason": r.reason, "count": r.n} for r in db.all(
        f"SELECT TOP 8 e.parameter, e.reason, COUNT(*) AS n FROM {T.case_exceptions} e {join} GROUP BY e.parameter, e.reason ORDER BY COUNT(*) DESC",
        eargs if since else [])]
    waiting = db.scalar(f"SELECT COUNT(*) FROM {T.cases} WHERE review_status = 'PENDING'{wsql}", wargs)

    bucket_kind, series = trend(db, days, since, now)
    return {
        "days": days, "generated_at": now.isoformat(), "sla_hours": sla, "bucket": bucket_kind,
        "kpis": cur, "previous": prev,
        "funnel": {"received": cur["total"], "analyzed": cur["analyzed"], "auto_decided": cur["auto_decided"],
                   "sent_to_review": cur["needs_review"], "reviewed": cur["queue_reviewed"],
                   "waiting": waiting, "overturned": cur["overturned"]},
        "backlog": {"pending": pend.n or 0, "over_sla": pend.over_sla or 0,
                    "oldest": [{"id": c.id, "case_ref": c.case_ref, "filename": (c.media or {}).get("original_name") or c.case_ref,
                                "confidence": c.confidence, "since": c.analyzed_at} for c in oldest]},
        "trend": series, "confidence_histogram": bins, "top_exceptions": reasons,
        "tests": tests(db, since, None, rules),
        "engines": engines(),
    }
