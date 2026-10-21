"""Saved connections (Azure Blob Storage, Azure API, Omni DB, other).

Each connection has a type, a unique name, plain fields and encrypted secrets. One connection per type can be
*active* (the one the app uses). Only Azure Blob Storage is wired into the app today; the other types are stored
for the integrations that are coming and are clearly marked "not used yet" in the UI.
"""
import ipaddress
import logging
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import crypto
from .db import T, Db, Row

log = logging.getLogger(__name__)
MAX_LEN = 500


def F(key, label, kind="text", required=False, secret=False, options=None, default="", placeholder="", hint="", show_if=None):
    return {"key": key, "label": label, "kind": kind, "required": required, "secret": secret, "options": options or [],
            "default": default, "placeholder": placeholder, "hint": hint, "show_if": show_if}


TYPES: dict[str, dict] = {
    "azure_blob": {
        "label": "Azure Blob Storage", "used": True,
        "description": "Where uploaded videos and their playback copies are stored. The active connection is used for every new upload.",
        "fields": [
            F("auth_mode", "Authentication", "select", True, options=[["connection_string", "Connection string"], ["managed_identity", "Managed identity (recommended on Azure)"]], default="connection_string"),
            F("container", "Container", required=True, default="pivc-videos", placeholder="pivc-videos", hint="3-63 chars: lowercase letters, numbers, hyphens. Created automatically if missing."),
            F("prefix", "Folder prefix (optional)", placeholder="pivc/", hint="Blobs are named <prefix><case id>/video.mp4 and preview.mp4."),
            F("account_url", "Storage account URL", placeholder="https://myaccount.blob.core.windows.net", hint="The server's managed identity needs the 'Storage Blob Data Contributor' role.", show_if={"auth_mode": "managed_identity"}, required=True),
            F("connection_string", "Connection string", secret=True, required=True, placeholder="DefaultEndpointsProtocol=https;AccountName=...", show_if={"auth_mode": "connection_string"}),
        ],
    },
    "openai": {
        "label": "OpenAI", "used": True,
        "description": "The AI model that writes the plain-language interpretation of a video's results and the overview in PDF reports. "
                       "It is sent only the tests' statuses, scores and reasons: never the video, the ID photo, file names or customer details. "
                       "Works with OpenAI or with an Azure OpenAI deployment.",
        "fields": [
            F("provider", "Provider", "select", True, options=[["openai", "OpenAI"], ["azure", "Azure OpenAI"]], default="openai"),
            F("model", "Model", required=True, placeholder="e.g. gpt-4o", hint="For Azure OpenAI, enter the deployment name."),
            F("endpoint", "Azure OpenAI endpoint", placeholder="https://myresource.openai.azure.com", required=True, show_if={"provider": "azure"}),
            F("api_version", "API version", placeholder="2024-10-21", show_if={"provider": "azure"}),
            F("base_url", "API base URL (optional)", placeholder="https://api.openai.com/v1", hint="Leave empty for OpenAI itself. Must be https.", show_if={"provider": "openai"}),
            F("api_key", "API key", secret=True, required=True),
        ],
    },
    "azure_api": {
        "label": "Azure API", "used": False,
        "description": "Endpoint and key of an Azure AI / Cognitive Services resource. Stored for upcoming analysis features; not used yet.",
        "fields": [
            F("endpoint", "Endpoint URL", required=True, placeholder="https://myresource.cognitiveservices.azure.com"),
            F("api_key", "API key", secret=True, required=True),
            F("region", "Region", placeholder="centralindia"),
            F("api_version", "API version", placeholder="2024-02-01"),
            F("deployment", "Deployment / model name"),
        ],
    },
    "omni_db": {
        "label": "Omni DB", "used": False,
        "description": "Connection to the Omni database that holds PIVC videos and case data. Stored for the upcoming integration; not used yet.",
        "fields": [
            F("db_type", "Database type", "select", True, options=[["mssql", "SQL Server"], ["postgresql", "PostgreSQL"], ["mysql", "MySQL"]], default="mssql"),
            F("host", "Host", required=True),
            F("port", "Port", placeholder="1433"),
            F("database", "Database", required=True),
            F("username", "Username", required=True),
            F("password", "Password", secret=True, required=True),
        ],
    },
    "custom": {
        "label": "Other", "used": False,
        "description": "Any other service the solution will need to call. Stored securely; not used yet.",
        "fields": [
            F("base_url", "Base URL"),
            F("api_key", "API key / token", secret=True),
            F("notes", "Notes", hint="What this connection is for."),
        ],
    },
}


class ConnectionInputError(ValueError):
    pass


# ---- validation ----------------------------------------------------------------------------------
def _schema(type_: str) -> dict:
    if type_ not in TYPES:
        raise ConnectionInputError(f"Unknown connection type '{type_}'.")
    return TYPES[type_]


def _visible(field: dict, fields: dict) -> bool:
    cond = field["show_if"]
    return not cond or all(fields.get(k) == v for k, v in cond.items())


def clean(type_: str, fields: dict, secrets: dict, existing_secrets: dict | None = None) -> tuple[dict, dict]:
    """Validate input -> (fields, secrets) to store. Missing secret keys keep their existing value."""
    schema = _schema(type_)
    out_f, out_s = {}, dict(existing_secrets or {})
    for f in schema["fields"]:
        k = f["key"]
        if f["secret"]:
            v = secrets.get(k)
            if isinstance(v, str) and v.strip():
                out_s[k] = v.strip()
            continue
        v = str(fields.get(k, f["default"]) or "").strip()
        if len(v) > MAX_LEN:
            raise ConnectionInputError(f"{f['label']} is too long.")
        if f["kind"] == "select" and v not in [o[0] for o in f["options"]]:
            raise ConnectionInputError(f"Invalid value for {f['label']}.")
        out_f[k] = v
    for f in schema["fields"]:
        if f["required"] and _visible(f, out_f):
            have = out_s.get(f["key"]) if f["secret"] else out_f.get(f["key"])
            if not have and not (type_ == "azure_blob" and f["key"] == "connection_string" and _env_conn()):
                raise ConnectionInputError(f"{f['label']} is required.")
    # secrets of fields hidden by the current mode are dropped
    for f in schema["fields"]:
        if f["secret"] and not _visible(f, out_f):
            out_s.pop(f["key"], None)
    if type_ == "azure_blob":
        from .storage import validate_blob
        validate_blob(out_f)
    if type_ in ("azure_api", "custom", "openai"):
        for k in ("endpoint", "base_url"):
            if out_f.get(k) and not out_f[k].lower().startswith("https://"):
                raise ConnectionInputError("The URL must start with https://")
    return out_f, out_s


def _env_conn() -> str:
    from .config import settings
    return settings.azure_storage_connection_string


# ---- persistence ---------------------------------------------------------------------------------
def secrets_of(row: Row) -> dict:
    return crypto.decrypt_dict(row.secrets_enc) or {}


def create(db: Db, type_: str, name: str, fields: dict, secrets: dict) -> Row:
    name = name.strip()
    if not name or len(name) > 100:
        raise ConnectionInputError("Name is required (max 100 characters).")
    if db.scalar(f"SELECT COUNT(*) FROM {T.connections} WHERE type = ? AND name = ?", (type_, name)):
        raise ConnectionInputError(f"A {TYPES[type_]['label']} connection named '{name}' already exists.")
    f, s = clean(type_, fields, secrets)
    if s and not crypto.can_store_secret():
        raise ConnectionInputError("Set APP_SECRET_KEY on the server to save secrets here.")
    # the first connection of a type becomes the active one
    first = not db.scalar(f"SELECT COUNT(*) FROM {T.connections} WHERE type = ? AND active = 1", (type_,))
    row = db.insert("connections", type=type_, name=name, fields=f, secrets_enc=crypto.encrypt_dict(s) if s else None, active=first)
    db.commit()
    return row


def update(db: Db, row: Row, name: str | None, fields: dict, secrets: dict, clear: list[str]) -> Row:
    if name is not None and name.strip() != row.name:
        name = name.strip()
        if not name or len(name) > 100:
            raise ConnectionInputError("Name is required (max 100 characters).")
        if db.scalar(f"SELECT COUNT(*) FROM {T.connections} WHERE type = ? AND name = ? AND id <> ?", (row.type, name, row.id)):
            raise ConnectionInputError(f"A connection named '{name}' already exists.")
        row.name = name
    existing = secrets_of(row)
    if row.secrets_enc and not existing and secrets:
        existing = {}  # saved secrets can't be decrypted (key changed): replaced by what was entered
    for k in clear:
        existing.pop(k, None)
    f, s = clean(row.type, {**row.fields, **fields}, secrets, existing)
    if s and not crypto.can_store_secret():
        raise ConnectionInputError("Set APP_SECRET_KEY on the server to save secrets here.")
    row.fields, row.secrets_enc = f, (crypto.encrypt_dict(s) if s else None)
    row.updated_at = datetime.now(timezone.utc)
    db.save(row)
    db.commit()
    return row


def activate(db: Db, row: Row) -> None:
    db.run(f"UPDATE {T.connections} SET active = 0 WHERE type = ? AND active = 1", (row.type,))
    row.active = True
    db.save(row)
    db.commit()


def active(db: Db, type_: str) -> Row | None:
    return db.one(f"SELECT TOP 1 * FROM {T.connections} WHERE type = ? AND active = 1", (type_,), "connections")


def get(db: Db, id_: int) -> Row | None:
    return db.get("connections", id_)


def list_all(db: Db, type_: str | None = None) -> list[Row]:
    sql = f"SELECT * FROM {T.connections}" + (" WHERE type = ?" if type_ else "") + " ORDER BY type, name"
    return db.all(sql, (type_,) if type_ else (), "connections")


def delete(db: Db, row: Row) -> None:
    db.delete("connections", "id = ?", (row.id,))
    db.commit()


def summary(row: Row) -> str:
    f = row.fields
    if row.type == "azure_blob":
        acct = f.get("account_url", "").replace("https://", "").split(".")[0]
        if f.get("auth_mode") == "connection_string":
            import re
            m = re.search(r"AccountName=([^;]+)", secrets_of(row).get("connection_string") or _env_conn() or "")
            acct = m.group(1) if m else "?"
        return f"{acct} / {f.get('container')}" + (f" / {f['prefix']}" if f.get("prefix") else "")
    if row.type == "azure_api":
        return f"{f.get('endpoint', '')}" + (f" ({f['deployment']})" if f.get("deployment") else "")
    if row.type == "omni_db":
        return f"{f.get('db_type')}://{f.get('host')}{':' + f['port'] if f.get('port') else ''}/{f.get('database')} as {f.get('username')}"
    return f.get("base_url") or f.get("notes") or ""


def public(row: Row) -> dict:
    schema = TYPES[row.type]
    stored = secrets_of(row)
    secret_state = {}
    for fld in schema["fields"]:
        if fld["secret"] and _visible(fld, row.fields):
            if stored.get(fld["key"]):
                secret_state[fld["key"]] = {"set": True, "source": "saved"}
            elif row.type == "azure_blob" and _env_conn():
                secret_state[fld["key"]] = {"set": True, "source": "environment"}
            else:
                secret_state[fld["key"]] = {"set": False, "source": "none", "undecryptable": bool(row.secrets_enc)}
    return {"id": row.id, "type": row.type, "name": row.name, "active": row.active, "fields": row.fields,
            "secrets": secret_state, "summary": summary(row), "updated_at": row.updated_at}


# ---- testing -------------------------------------------------------------------------------------
def _check_public_https(url: str) -> str:
    """Server-side requests to user-supplied URLs: https only, no private/loopback/link-local targets."""
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ConnectionInputError("The URL must start with https://")
    for info in socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ConnectionInputError("That address resolves to a private or internal network, which is not allowed here.")
    return url


def _http_probe(url: str, key: str | None) -> dict:
    _check_public_https(url)
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": key or "", "api-key": key or "", "User-Agent": "pivc-connection-test"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 - scheme and address checked above
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    if code in (401, 403):
        return {"ok": False, "message": f"The endpoint is reachable but rejected the credentials (HTTP {code})."}
    return {"ok": True, "message": f"Endpoint reachable (HTTP {code}). This check cannot verify every service's credentials."}


def _try_database(f: dict, password: str) -> None:
    """Open a connection to a customer database with its own driver and run SELECT 1."""
    kind, host, port, db_name, user = f["db_type"], f["host"], f.get("port"), f["database"], f["username"]
    if kind == "mssql":
        import mssql_python
        server = f"tcp:{host},{port}" if port else f"tcp:{host}"
        conn = mssql_python.connect(f"Server={server};Database={db_name};UID={user};PWD={{{password.replace('}', '}}')}}};Encrypt=yes;TrustServerCertificate=no;", timeout=8)
    elif kind == "postgresql":
        import psycopg
        conn = psycopg.connect(host=host, port=int(port) if port else 5432, dbname=db_name, user=user, password=password, connect_timeout=8)
    else:
        raise ModuleNotFoundError(f"no driver for {kind}", name=kind)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
    finally:
        conn.close()


def test(type_: str, fields: dict, secrets: dict) -> dict:
    """Try a connection with the given (already merged) values. Nothing is saved."""
    f, s = clean(type_, fields, secrets)
    try:
        if type_ == "azure_blob":
            from .storage import AzureBlobStore
            cfg = {**f}
            conn = s.get("connection_string") or _env_conn()
            AzureBlobStore(cfg, conn).check()
            return {"ok": True, "message": f"Wrote, read back and deleted a test blob in container '{f['container']}'."}
        if type_ == "openai":
            from . import llm
            return {"ok": True, "message": llm.check({**f, "api_key": s.get("api_key") or ""})}
        if type_ == "azure_api":
            return _http_probe(f["endpoint"], s.get("api_key"))
        if type_ == "omni_db":
            _try_database(f, s.get("password") or "")
            return {"ok": True, "message": "Connected and ran SELECT 1."}
        if f.get("base_url"):
            return _http_probe(f["base_url"], s.get("api_key"))
        return {"ok": True, "message": "Nothing to test for this connection."}
    except ConnectionInputError as e:
        return {"ok": False, "message": str(e)}
    except RuntimeError as e:  # llm.LlmError: its messages are written for the administrator and never contain the key
        return {"ok": False, "message": str(e)}
    except ModuleNotFoundError as e:
        return {"ok": False, "message": f"Database driver not installed on the server: {e.name}"}
    except Exception as e:  # noqa: BLE001 - surface the reason to the admin (never contains the secret)
        return {"ok": False, "message": f"{type(e).__name__}: {str(e)[:300]}"}
