"""The compliance trail. `record` writes one ActivityLog row; `case_event` also adds it to the video's own timeline."""
from .db import T, Db, Row

# which group an action belongs to on the Audit log page
CATEGORY = {
    "uploaded": "video", "analyzed": "system", "re-analyzed": "system", "re-run failed": "system", "re-run requested": "video", "deleted": "video",
    "stored in blob storage": "system", "blob storage failed": "system", "reviewed": "review",
    "interpretation generated": "video", "video report downloaded": "reports",
}
LABELS = {"video": "Videos", "review": "Review decisions", "rules": "Rules", "settings": "Configuration", "users": "Users",
          "security": "Sign-in", "reports": "Reports", "system": "System"}


def record(db: Db, actor: str, category: str, action: str, target: str = "", detail: dict | None = None,
           case_id: int | None = None, ip: str = "", at=None) -> None:
    """Writes the event in the current transaction; the caller commits (so it lands together with the change itself)."""
    db.insert("activity_log", actor=actor[:120], category=category, action=action[:64], target=str(target)[:160],
              case_id=case_id, detail=detail or {}, ip=ip[:45], **({"at": at} if at else {}))


def case_event(db: Db, case: Row, actor: str, action: str, detail: dict | None = None) -> None:
    db.insert("audit_log", case_id=case.id, actor=actor, action=action, detail=detail or {})
    record(db, actor, CATEGORY.get(action, "video"), action, case.case_ref,
           {"filename": (case.media or {}).get("original_name"), **(detail or {})}, case.id)


def client_ip(request) -> str:
    return request.client.host if request is not None and request.client else ""


def backfill(db: Db) -> int:
    """If the activity log is empty but per-video history exists, copy it over so it is not lost (used after a data import)."""
    if db.scalar(f"SELECT COUNT(*) FROM {T.activity_log}"):
        return 0
    n = 0
    rows = db.all(f"SELECT a.*, c.case_ref AS ref, c.media AS media FROM {T.audit_log} a JOIN {T.cases} c ON c.id = a.case_id ORDER BY a.id", (), "audit_log")
    for a in rows:
        media = a.get("media")
        media = __import__("json").loads(media) if isinstance(media, str) else (media or {})
        record(db, a.actor, CATEGORY.get(a.action, "video"), a.action, a.ref, {"filename": media.get("original_name"), **(a.detail or {})}, a.case_id, at=a.at)
        n += 1
    db.commit()
    return n


def describe(a: Row) -> str:
    """One readable sentence for the log page and the CSV."""
    d, t = a.detail or {}, a.target
    name = d.get("filename") or t
    match a.action:
        case "uploaded": return f"Uploaded {name} ({t})"
        case "analyzed":
            c = d.get("confidence")
            return f"Analysis finished for {t}: {d.get('decision')}" + (f" ({c:.0%} confidence)" if isinstance(c, (int, float)) else "")
        case "re-run requested":
            tests = d.get("tests")
            return f"Re-ran {', '.join(tests) if tests else 'all tests'} of {name} ({t})"
        case "re-analyzed":
            ch = [f"{k} {d['before'].get(k, '–')} → {v}" for k, v in (d.get("after") or {}).items()]
            return f"Re-run finished for {t}: " + "; ".join(ch) + (f". Decision {d.get('decision_before')} → {d.get('decision_after')}" if d.get("decision_before") != d.get("decision_after") else ". Decision unchanged")
        case "re-run failed": return f"Re-running {', '.join(d.get('tests', []))} failed for {t}; earlier results kept: {d.get('error')}"
        case "deleted":
            outcome = d.get("final_decision") or d.get("decision")
            return f"Deleted {name} ({t})" + (f", which had been decided {outcome}" if outcome else "")
        case "reviewed":
            return f"Decided {t}: {d.get('final_decision')} (system said {d.get('ai_decision')})" + (f". Note: {d['notes']}" if d.get("notes") else "")
        case "stored in blob storage": return f"Moved {t} to blob storage"
        case "blob storage failed": return f"Could not move {t} to blob storage"
        case "rules changed":
            ch = d.get("changes", [])
            return f"Changed rules (version {t}): " + "; ".join(f"{c['key']} {c['from']} → {c['to']}" for c in ch[:6]) + (" …" if len(ch) > 6 else "")
        case "rules restored": return f"Restored the rules of version {d.get('restored_from')} as version {t}"
        case "signed in": return f"{a.actor} signed in"
        case "sign-in failed": return f"Failed sign-in for “{t}” ({d.get('reason', 'wrong password')})"
        case "account locked": return f"Account {t} was locked after repeated failures"
        case "signed out": return f"{a.actor} signed out"
        case "password changed": return f"{a.actor} changed their password"
        case "first administrator created": return f"Created the first administrator {t}"
        case "user created": return f"Created user {t} as {d.get('role')}"
        case "user changed": return f"Changed user {t}: " + ", ".join(f"{k} {v['from']} → {v['to']}" for k, v in d.get("changes", {}).items())
        case "password reset": return f"Reset the password of {t}"
        case "user unlocked": return f"Unlocked {t}"
        case "user deleted": return f"Deleted user {t}"
        case "connection created": return f"Added the {d.get('type')} connection “{t}”"
        case "connection updated":
            return f"Edited the {d.get('type')} connection “{t}”" + (f" ({', '.join(d['changed'])})" if d.get("changed") else "")
        case "connection activated": return f"Made “{t}” the active {d.get('type')} connection"
        case "connection deactivated": return f"Deactivated the {d.get('type')} connection “{t}”"
        case "connection deleted": return f"Deleted the {d.get('type')} connection “{t}”"
        case "interpretation generated": return f"Generated an AI interpretation of {name} ({t})"
        case "video report downloaded": return f"Downloaded the PDF report of {name} ({t})"
        case "report exported": return f"Exported a {str(d.get('format', '')).upper()} report of {d.get('rows')} videos"
        case "audit exported": return f"Exported {d.get('rows')} audit log entries"
    return f"{a.action} {t}".strip()
