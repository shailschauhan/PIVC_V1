"""Azure SQL access, written directly against the Microsoft driver (mssql-python): plain T-SQL, no ORM.

`Db` is one transaction on one pooled connection. Rows come back as `Row` (a dict you can also read as attributes); JSON,
boolean and UTC-datetime columns are converted using the table definitions in schema.py. Writing is explicit:

    row = db.one("SELECT * FROM " + T.cases + " WHERE id = ?", (5,), "cases")
    row.status = "PENDING"        # marks the field changed
    db.save(row)                  # UPDATE only the changed fields
    db.commit()

All tables live in their own schema (SQL_SCHEMA, default "pivc"), so they never touch the other applications in the database.
"""
import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

import mssql_python

from .config import settings

log = logging.getLogger(__name__)
SCHEMA = settings.sql_schema


class _Tables:
    """`T.cases` -> "[pivc].[cases]": a table name qualified with the schema, for use inside SQL text."""

    def __getattr__(self, name: str) -> str:
        return f"[{SCHEMA}].[{name}]"


T = _Tables()


# ---- connection string ---------------------------------------------------------------------------------------------------
_KEYS = {"server": "Server", "data source": "Server", "address": "Server", "initial catalog": "Database", "database": "Database",
         "user id": "UID", "uid": "UID", "user": "UID", "password": "PWD", "pwd": "PWD", "encrypt": "Encrypt",
         "trustservercertificate": "TrustServerCertificate", "authentication": "Authentication"}
_TIMEOUT = ("connection timeout", "connect timeout", "timeout")


def parse_connection_string(cs: str) -> tuple[str, int]:
    """Accepts the ADO.NET style string Azure shows (Server=tcp:...;Initial Catalog=...;User ID=...;Password=...) or the
    driver's own keywords. Returns (driver connection string, timeout seconds)."""
    out: dict[str, str] = {}
    timeout = 30
    for part in _split(cs):
        key, _, value = part.partition("=")
        k = key.strip().lower()
        if k in _TIMEOUT:
            timeout = int(value.strip() or 30)
        elif k in _KEYS:
            v = value.strip()
            if _KEYS[k] in ("Encrypt", "TrustServerCertificate"):
                v = {"true": "yes", "false": "no"}.get(v.lower(), v)
            out[_KEYS[k]] = v
    if "Server" not in out or "Database" not in out:
        raise ValueError("SQL_CONNECTION_STRING needs a Server and a Database (Initial Catalog)")
    out.setdefault("Encrypt", "yes")
    out.setdefault("TrustServerCertificate", "no")
    return ";".join(f"{k}={_brace(v) if k == 'PWD' else v}" for k, v in out.items()) + ";", timeout


def _split(cs: str) -> list[str]:
    parts, cur, brace = [], "", False
    for ch in cs:
        if ch == "{":
            brace = True
        elif ch == "}":
            brace = False
        if ch == ";" and not brace:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [p for p in parts + [cur] if p.strip()]


def _brace(v: str) -> str:
    v = v[1:-1] if v.startswith("{") and v.endswith("}") else v
    return "{" + v.replace("}", "}}") + "}"


# ---- pool ------------------------------------------------------------------------------------------------------------------
class _Pool:
    """A few long-lived connections, handed out one per request or worker. Azure SQL drops idle connections, so one that
    has sat unused for a while is tested before it is reused, and replaced if it is dead."""
    IDLE_CHECK_S = 30

    def __init__(self) -> None:
        self._free: queue.LifoQueue = queue.LifoQueue()
        self._lock = threading.Lock()
        self._made = 0

    def _connect(self):
        if not settings.sql_connection_string:
            raise RuntimeError("SQL_CONNECTION_STRING is not set. Put the Azure SQL connection string in backend/.env.")
        cs, timeout = parse_connection_string(settings.sql_connection_string)
        return mssql_python.connect(cs, timeout=timeout)

    def acquire(self):
        try:
            conn, last = self._free.get_nowait()
        except queue.Empty:
            with self._lock:
                make = self._made < settings.sql_pool_size
                if make:
                    self._made += 1
            if not make:
                conn, last = self._free.get(timeout=60)
            else:
                try:
                    return self._connect()
                except Exception:
                    with self._lock:
                        self._made -= 1
                    raise
        if time.monotonic() - last > self.IDLE_CHECK_S:
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchall()
                cur.close()
            except Exception:  # noqa: BLE001 - dropped by the server: replace it
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = self._connect()
        return conn

    def release(self, conn, broken: bool = False) -> None:
        if broken:
            with self._lock:
                self._made -= 1
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return
        self._free.put((conn, time.monotonic()))


_pool = _Pool()


# ---- rows --------------------------------------------------------------------------------------------------------------------
class Row(dict):
    """A table row: read fields as row["x"] or row.x; assigning row.x = v records the change for Db.save()."""
    __slots__ = ("_table", "_dirty")

    def __init__(self, data: dict, table: str | None = None):
        super().__init__(data)
        object.__setattr__(self, "_table", table)
        object.__setattr__(self, "_dirty", set())

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value
        self._dirty.add(name)


def _utc(v: datetime) -> datetime:
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)


def _bind(v: Any) -> Any:
    """Python value -> something the driver accepts."""
    if isinstance(v, datetime):
        return _utc(v).replace(tzinfo=None)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return v


def like(text: str) -> str:
    """A LIKE pattern matching `text` anywhere, with its wildcard characters taken literally. Use it with ESCAPE '!'."""
    for ch in ("!", "%", "_", "["):
        text = text.replace(ch, "!" + ch)
    return "%" + text + "%"


def marks(n: int) -> str:
    """'?, ?, ?' for an IN (...) list."""
    return ", ".join("?" * n)


class Db:
    """One transaction. Use `with open_db() as db:` (workers, scripts) or the `get_db` dependency (requests)."""

    def __init__(self) -> None:
        self._conn = _pool.acquire()
        self._broken = False

    # -- running SQL
    def _exec(self, sql: str, params: Iterable = ()):
        cur = self._conn.cursor()
        try:
            cur.execute(sql, [_bind(p) for p in params])
        except mssql_python.Error as e:
            self._broken = self._broken or "08" in str(getattr(e, "sqlstate", "")) or "connection" in str(e).lower()
            cur.close()
            raise
        return cur

    def run(self, sql: str, params: Iterable = ()) -> int:
        cur = self._exec(sql, params)
        n = cur.rowcount
        cur.close()
        return n

    def _rows(self, cur, table: str | None) -> list[Row]:
        if cur.description is None:
            return []
        names = [d[0] for d in cur.description]
        return [self._row(dict(zip(names, r)), table) for r in cur.fetchall()]

    def _row(self, data: dict, table: str | None) -> Row:
        from .schema import TABLES
        spec = TABLES.get(table) if table else None
        for k, v in data.items():
            if isinstance(v, datetime):
                data[k] = _utc(v)
            elif spec is not None and v is not None:
                kind = spec.kinds.get(k)
                if kind == "json":
                    data[k] = json.loads(v) if isinstance(v, str) else v
                elif kind == "bool":
                    data[k] = bool(v)
        return Row(data, table)

    def all(self, sql: str, params: Iterable = (), table: str | None = None) -> list[Row]:
        cur = self._exec(sql, params)
        try:
            return self._rows(cur, table)
        finally:
            cur.close()

    def one(self, sql: str, params: Iterable = (), table: str | None = None) -> Row | None:
        rows = self.all(sql, params, table)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable = ()) -> Any:
        cur = self._exec(sql, params)
        try:
            r = cur.fetchone()
            return r[0] if r else None
        finally:
            cur.close()

    def stream(self, sql: str, params: Iterable = (), table: str | None = None, size: int = 500) -> Iterator[Row]:
        """Rows in batches, so a very large result is never held in memory at once."""
        cur = self._exec(sql, params)
        try:
            names = [d[0] for d in cur.description]
            while True:
                batch = cur.fetchmany(size)
                if not batch:
                    return
                for r in batch:
                    yield self._row(dict(zip(names, r)), table)
        finally:
            cur.close()

    # -- writing rows
    def get(self, table: str, id_: Any) -> Row | None:
        return self.one(f"SELECT * FROM {T.__getattr__(table)} WHERE id = ?", (id_,), table)

    def insert(self, table: str, **values) -> Row:
        """Insert one row (defaults filled in) and return it with its new id."""
        from .schema import TABLES
        spec = TABLES[table]
        data = spec.fill(values)
        cols = list(data)
        cur = self._exec(f"INSERT INTO {T.__getattr__(table)} ({', '.join(f'[{c}]' for c in cols)}) OUTPUT INSERTED.[id] "
                         f"VALUES ({marks(len(cols))})", [spec.to_sql(c, data[c]) for c in cols])
        try:
            data["id"] = cur.fetchone()[0]
        finally:
            cur.close()
        return Row(data, table)

    def save(self, row: Row) -> None:
        """UPDATE the fields changed since the row was read (or last saved)."""
        from .schema import TABLES
        if not row._dirty:
            return
        spec = TABLES[row._table]
        cols = sorted(row._dirty)
        self.run(f"UPDATE {T.__getattr__(row._table)} SET {', '.join(f'[{c}] = ?' for c in cols)} WHERE id = ?",
                 [spec.to_sql(c, row[c]) for c in cols] + [row["id"]])
        row._dirty.clear()

    def delete(self, table: str, where: str = "", params: Iterable = ()) -> int:
        return self.run(f"DELETE FROM {T.__getattr__(table)}" + (f" WHERE {where}" if where else ""), params)

    # -- transaction
    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # noqa: BLE001
            self._broken = True

    def close(self) -> None:
        if self._conn is None:
            return
        if not self._broken:
            self.rollback()  # anything not committed is discarded
        _pool.release(self._conn, self._broken)
        self._conn = None


@contextmanager
def open_db() -> Iterator[Db]:
    db = Db()
    try:
        yield db
    finally:
        db.close()


def get_db() -> Iterator[Db]:
    """FastAPI dependency: one transaction per request."""
    with open_db() as db:
        yield db
