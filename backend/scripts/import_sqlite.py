"""One-off: copy the data from the old local SQLite file (pivc.db) into Azure SQL.

    python scripts/import_sqlite.py pivc.db            # refuses if the Azure SQL tables already hold data
    python scripts/import_sqlite.py pivc.db --sessions # also carry over sign-in sessions (default: users sign in again)

Ids are kept, so links between rows (a video and its results) stay intact. Password hashes and encrypted connection
secrets are copied as they are, so the same APP_SECRET_KEY keeps decrypting them. The SQLite file is only read.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import schema  # noqa: E402
from app.db import T, open_db  # noqa: E402

ORDER = ["users", "connections", "rule_config", "cases", "case_params", "case_exceptions", "audit_log", "deletion_log",
         "activity_log", "engine_calls", "user_sessions"]


def _convert(kind: str, v):
    if v is None:
        return None
    if kind == "dt":  # SQLite kept naive UTC text
        d = datetime.fromisoformat(v) if isinstance(v, str) else v
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    if kind == "bool":
        return bool(v)
    if kind == "json":
        return v if isinstance(v, str) else json.dumps(v)
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite_file")
    ap.add_argument("--sessions", action="store_true")
    args = ap.parse_args()
    src = sqlite3.connect(f"file:{args.sqlite_file}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    with open_db() as db:
        schema.ensure(db)
        busy = [t for t in ORDER if db.scalar(f"SELECT COUNT(*) FROM {getattr(T, t)}")]
        if busy:
            print(f"Azure SQL already has data in: {', '.join(busy)}. Nothing was copied.")
            return 1
        total = 0
        for name in ORDER:
            if name not in have or (name == "user_sessions" and not args.sessions):
                continue
            spec = schema.TABLES[name]
            cols = ["id"] + [c[0] for c in spec.cols]
            present = {r[1] for r in src.execute(f"PRAGMA table_info({name})")}
            use = [c for c in cols if c in present]
            rows = src.execute(f"SELECT {', '.join(use)} FROM {name} ORDER BY id").fetchall()
            if not rows:
                continue
            kinds = {"id": "int", **spec.kinds}
            marks = ", ".join("?" * len(use))
            db.run(f"SET IDENTITY_INSERT {getattr(T, name)} ON")
            for r in rows:
                db.run(f"INSERT INTO {getattr(T, name)} ({', '.join(f'[{c}]' for c in use)}) VALUES ({marks})",
                       [_convert(kinds[c], r[c]) for c in use])
            db.run(f"SET IDENTITY_INSERT {getattr(T, name)} OFF")
            db.commit()
            print(f"{name:16s} {len(rows)} rows")
            total += len(rows)
    print(f"Copied {total} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
