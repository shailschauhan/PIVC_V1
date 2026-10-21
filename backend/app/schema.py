"""The tables, as data: one definition drives creating them, adding columns later, and converting values to and from SQL.

`ensure()` is safe to run on every start: it creates what is missing and adds columns that newer versions expect.
There are no foreign keys on purpose: the application deletes a video's rows itself, and it keeps the audit and
activity logs after the things they describe are gone.
"""
import json
from datetime import datetime, timezone
from typing import Any, Callable

from .db import SCHEMA, Db, open_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


_NODEFAULT = object()


class Table:
    def __init__(self, name: str, cols: list[tuple], indexes: list[tuple] | None = None):
        self.name = name
        self.cols: list[tuple[str, str, bool, Any]] = cols   # (name, type, nullable, default)
        self.indexes = indexes or []                         # (columns tuple, unique)
        self.kinds = {c[0]: c[1].split(":")[0] for c in cols}

    def fill(self, values: dict) -> dict:
        """The values for an INSERT: what was given plus defaults; unknown names are an error (a typo must not vanish)."""
        known = {c[0] for c in self.cols}
        unknown = set(values) - known
        if unknown:
            raise KeyError(f"{self.name}: unknown column(s) {sorted(unknown)}")
        out = {}
        for name, _typ, nullable, default in self.cols:
            if name in values:
                out[name] = values[name]
            elif default is not _NODEFAULT:
                out[name] = default() if callable(default) else (default.copy() if isinstance(default, (dict, list)) else default)
            elif nullable:
                out[name] = None
            else:
                raise KeyError(f"{self.name}.{name} is required")
        return out

    def to_sql(self, col: str, value: Any) -> Any:
        if value is None:
            return None
        if self.kinds[col] == "json":
            return json.dumps(value, default=str)
        return value

    def ddl_type(self, typ: str) -> str:
        kind, _, arg = typ.partition(":")
        return {"int": "INT", "float": "FLOAT", "bool": "BIT", "dt": "DATETIME2(6)", "text": "NVARCHAR(MAX)", "json": "NVARCHAR(MAX)",
                "str": f"NVARCHAR({arg or 255})"}[kind]


def C(name: str, typ: str, null: bool = False, default: Any = _NODEFAULT) -> tuple:
    return (name, typ, null, default)


TABLES: dict[str, Table] = {t.name: t for t in [
    Table("cases", [
        C("case_ref", "str:64"), C("proposal_no", "str:64"), C("customer_id", "str:64"), C("customer_name", "str:200"),
        C("source", "str:32", default="omni"), C("declared", "json", default=dict), C("media", "json", default=dict),
        C("status", "str:16", default="PENDING"), C("error", "text", True), C("report", "json", True),
        C("decision", "str:16", True), C("confidence", "float", True), C("processing_ms", "int", True),
        C("review_status", "str:16", default="NOT_REQUIRED"), C("final_decision", "str:16", True), C("reviewer", "str:120", True),
        C("review_notes", "text", True), C("reviewed_at", "dt", True), C("created_at", "dt", default=_now), C("analyzed_at", "dt", True),
        C("interpretation", "json", True), C("interpretation_at", "dt", True),
    ], [(("case_ref",), True), (("status",), False), (("decision",), False), (("review_status",), False), (("created_at",), False),
        (("proposal_no",), False), (("customer_id",), False)]),
    Table("audit_log", [C("case_id", "int"), C("actor", "str:120"), C("action", "str:64"), C("detail", "json", default=dict), C("at", "dt", default=_now)],
          [(("case_id",), False)]),
    Table("rule_config", [C("rules", "json"), C("updated_at", "dt", default=_now), C("updated_by", "str:120", True), C("note", "str:300", True)]),
    Table("case_params", [C("case_id", "int"), C("parameter", "str:40"), C("status", "str:40"), C("confidence", "float"), C("score", "float", True),
                          C("engine", "str:20", default="simulated")],
          [(("case_id",), False), (("parameter", "status"), False)]),
    Table("case_exceptions", [C("case_id", "int"), C("parameter", "str:40"), C("reason", "str:200"), C("message", "text")],
          [(("case_id",), False), (("parameter", "reason"), False)]),
    Table("connections", [C("type", "str:32"), C("name", "str:100"), C("fields", "json", default=dict), C("secrets_enc", "text", True),
                          C("active", "bool", default=False), C("created_at", "dt", default=_now), C("updated_at", "dt", default=_now)],
          [(("type", "name"), True), (("type",), False)]),
    Table("deletion_log", [C("case_ref", "str:64"), C("filename", "str:300"), C("reference", "str:64", True), C("decision", "str:16", True),
                           C("final_decision", "str:16", True), C("stored_in", "str:16", default="local"), C("deleted_by", "str:120"),
                           C("deleted_at", "dt", default=_now)], [(("case_ref",), False)]),
    Table("users", [C("username", "str:40"), C("full_name", "str:120"), C("role", "str:16", default="viewer"), C("active", "bool", default=True),
                    C("password_hash", "str:300"), C("must_change_password", "bool", default=False), C("failed_attempts", "int", default=0),
                    C("locked_until", "dt", True), C("created_by", "str:40", True), C("created_at", "dt", default=_now), C("last_login_at", "dt", True)],
          [(("username",), True)]),
    Table("user_sessions", [C("token_hash", "str:64"), C("user_id", "int"), C("created_at", "dt", default=_now), C("expires_at", "dt"),
                            C("ip", "str:45", default=""), C("user_agent", "str:200", default="")],
          [(("token_hash",), True), (("user_id",), False), (("expires_at",), False)]),
    Table("activity_log", [C("at", "dt", default=_now), C("actor", "str:120"), C("category", "str:16"), C("action", "str:64"),
                           C("target", "str:160", default=""), C("case_id", "int", True), C("detail", "json", default=dict), C("ip", "str:45", default="")],
          [(("at",), False), (("actor",), False), (("category",), False), (("action",), False), (("case_id",), False)]),
    Table("runs", [C("case_id", "int"), C("case_ref", "str:64"), C("filename", "str:300"), C("kind", "str:8"),  # first | all | tests
                   C("tests", "json", True), C("requested_by", "str:120"), C("requested_at", "dt", default=_now), C("started_at", "dt", True),
                   C("finished_at", "dt", True), C("status", "str:12", default="QUEUED"),  # QUEUED | RUNNING | DONE | FAILED | CANCELLED
                   C("before", "json", True), C("after", "json", True), C("decision_before", "str:16", True), C("decision_after", "str:16", True),
                   C("error", "str:300", True)],
          [(("status",), False), (("requested_at",), False), (("case_id",), False)]),
    Table("engine_calls", [C("at", "dt", default=_now), C("engine", "str:32"), C("ok", "bool", default=True), C("ms", "int", default=0),
                           C("error", "str:300", True)], [(("at",), False), (("engine",), False)]),
]}


def _q(t: str) -> str:
    return f"[{SCHEMA}].[{t}]"


def ensure(db: Db | None = None) -> list[str]:
    """Create the schema, tables, indexes and any columns added since the tables were first made. Returns what it added."""
    if db is None:
        with open_db() as own:
            return ensure(own)
    added = []
    db.run(f"IF SCHEMA_ID(N'{SCHEMA}') IS NULL EXEC(N'CREATE SCHEMA [{SCHEMA}]')")
    have = {(r[0], r[1]) for r in db.all("SELECT TABLE_NAME AS t, COLUMN_NAME AS c FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ?", (SCHEMA,))
            for r in [(r["t"], r["c"])]}
    tables = {t for t, _ in have}
    for t in TABLES.values():
        if t.name not in tables:
            cols = ", ".join(f"[{n}] {t.ddl_type(typ)} {'NULL' if null else 'NOT NULL'}" for n, typ, null, _d in t.cols)
            db.run(f"CREATE TABLE {_q(t.name)} ([id] INT IDENTITY(1,1) NOT NULL PRIMARY KEY, {cols})")
            added.append(t.name)
        else:
            for n, typ, null, default in t.cols:
                if (t.name, n) in have:
                    continue
                if not null and default is _NODEFAULT:
                    continue  # cannot be added to a populated table without a default; none of ours need it
                db.run(f"ALTER TABLE {_q(t.name)} ADD [{n}] {t.ddl_type(typ)} NULL")  # existing rows read as NULL; the app fills defaults on insert
                added.append(f"{t.name}.{n}")
    existing = {r["n"] for r in db.all("SELECT i.name AS n FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id "
                                       "JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = ?", (SCHEMA,))}
    for t in TABLES.values():
        for cols, unique in t.indexes:
            name = f"ix_{t.name}_{'_'.join(cols)}"
            if name not in existing:
                db.run(f"CREATE {'UNIQUE ' if unique else ''}INDEX [{name}] ON {_q(t.name)} ({', '.join(f'[{c}]' for c in cols)})")
    db.commit()
    return added


def _only_in_test_schema() -> None:
    if not SCHEMA.endswith("_test"):
        raise RuntimeError(f"Refusing to wipe tables in schema '{SCHEMA}': only schemas ending in _test may be cleared.")


def clear_all(db: Db) -> None:
    """Empty every table and restart the ids at 1 (used by the tests, in their own schema)."""
    _only_in_test_schema()
    db.run("; ".join(f"TRUNCATE TABLE {_q(t)}" for t in TABLES))
    db.commit()


def drop_all(db: Db) -> None:
    _only_in_test_schema()
    db.run("; ".join(f"DROP TABLE IF EXISTS {_q(t)}" for t in TABLES))
    db.commit()
