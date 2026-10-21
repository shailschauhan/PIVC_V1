"""Blob storage for uploaded media.

Flow: an upload is staged on local disk, analysed from there, then the original video and its browser
preview are copied to Azure Blob Storage and the local copies removed (only after the upload succeeded).
Cases store a `blob://<container>/<name>` URI; anything not starting with that is a local path.
Playback and re-analysis download a blob to a small local cache first.

The blob account comes from the *active* "Azure Blob Storage" connection (Configuration page): a connection
string (encrypted at rest, or the AZURE_STORAGE_CONNECTION_STRING environment variable) or Azure managed identity.
"""
import hashlib
import logging
import re
import shutil
import time
from pathlib import Path


from . import connections
from .db import Db
from .config import settings

log = logging.getLogger(__name__)
CACHE_MAX_AGE_S = 24 * 3600


class StorageError(RuntimeError):
    pass


def validate_blob(fields: dict) -> None:
    from .connections import ConnectionInputError
    c = fields["container"].strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]", c):
        raise ConnectionInputError("Container name must be 3-63 characters: lowercase letters, numbers and single hyphens.")
    if fields["auth_mode"] == "managed_identity" and not fields["account_url"].startswith("https://"):
        raise ConnectionInputError("Account URL (https://<account>.blob.core.windows.net) is required for managed identity.")
    if ".." in fields["prefix"] or fields["prefix"].startswith("/"):
        raise ConnectionInputError("Prefix must be a relative folder path such as 'pivc/'.")


# ---- Azure client --------------------------------------------------------------------------------
class AzureBlobStore:
    def __init__(self, cfg: dict, conn: str | None):
        from azure.storage.blob import BlobServiceClient
        if cfg["auth_mode"] == "connection_string":
            if not conn:
                raise StorageError("No connection string configured.")
            self.svc = BlobServiceClient.from_connection_string(conn)
        else:
            from azure.identity import DefaultAzureCredential
            self.svc = BlobServiceClient(cfg["account_url"], credential=DefaultAzureCredential())
        self.container, self.prefix = cfg["container"], cfg.get("prefix", "")

    def _client(self, container: str):
        return self.svc.get_container_client(container)

    def ensure_container(self) -> None:
        from azure.core.exceptions import ResourceExistsError
        try:
            self._client(self.container).create_container()
        except ResourceExistsError:
            pass

    def put_file(self, local: Path, key: str) -> str:
        name = f"{self.prefix}{key}"
        with local.open("rb") as f:
            self._client(self.container).upload_blob(name, f, overwrite=True, max_concurrency=4)
        return f"blob://{self.container}/{name}"

    def delete_blob(self, uri: str) -> None:
        """Delete one blob (a missing blob is fine: the goal is that it is gone)."""
        from azure.core.exceptions import ResourceNotFoundError
        container, name = uri[len("blob://"):].split("/", 1)
        try:
            self._client(container).delete_blob(name)
        except ResourceNotFoundError:
            pass

    def delete_folder_marker(self, uri: str) -> None:
        """Best effort: storage accounts with a hierarchical namespace keep an empty 0-byte object for each
        'folder' (the case folder) after its files are deleted. Remove that marker, but only if it really is one
        (zero bytes, hdi_isfolder) and its folder is empty. Never raises."""
        try:
            container, name = uri[len("blob://"):].split("/", 1)
            if "/" not in name:
                return
            folder = name.rsplit("/", 1)[0]
            blob = self._client(container).get_blob_client(folder)
            props = blob.get_blob_properties()
            if props.size == 0 and (props.metadata or {}).get("hdi_isfolder") == "true":
                blob.delete_blob()
        except Exception as e:  # noqa: BLE001 - not a hierarchical-namespace account, already gone, not empty...
            log.debug("folder marker for %s not removed: %s", uri, e)

    def get_file(self, uri: str, dest: Path) -> None:
        container, name = uri[len("blob://"):].split("/", 1)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with tmp.open("wb") as f:
            self._client(container).download_blob(name).readinto(f)
        tmp.replace(dest)

    def check(self) -> None:
        """Write, read back and delete a tiny blob: proves credentials, container and permissions."""
        self.ensure_container()
        c = self._client(self.container)
        name = f"{self.prefix}.pivc-connection-test"
        c.upload_blob(name, b"ok", overwrite=True)
        if c.download_blob(name).readall() != b"ok":
            raise StorageError("Read-back did not match what was written.")
        c.delete_blob(name)


_cache: dict[str, AzureBlobStore] = {}


def get_store(db: Db) -> AzureBlobStore | None:
    """The store for the active Azure Blob Storage connection, or None when there is none (local disk)."""
    row = connections.active(db, "azure_blob")
    if row is None:
        return None
    conn = connections.secrets_of(row).get("connection_string") or settings.azure_storage_connection_string or None
    key = hashlib.sha256(repr((row.id, sorted(row.fields.items()), conn)).encode()).hexdigest()
    if key not in _cache:
        _cache.clear()
        _cache[key] = AzureBlobStore(row.fields, conn)
    return _cache[key]


# ---- media helpers -------------------------------------------------------------------------------
def is_blob(uri: str | None) -> bool:
    return bool(uri) and uri.startswith("blob://")


def cache_dir() -> Path:
    return Path(settings.storage_dir) / "_cache"


def materialize(uri: str, db: Db) -> Path:
    """Local path for a media URI, downloading blobs to the cache first."""
    if not is_blob(uri):
        return Path(uri)
    dest = cache_dir() / uri[len("blob://"):]
    if not dest.is_file():
        store = get_store(db)
        if store is None:
            raise StorageError("This video is in blob storage, but no Azure Blob Storage connection is active.")
        store.get_file(uri, dest)
    return dest


def drop_cached(uri: str) -> None:
    """Remove the local cache copy of a blob (and its sibling preview/original), if any."""
    p = cache_dir() / uri[len("blob://"):]
    p.unlink(missing_ok=True)
    p.with_name(p.name + ".part").unlink(missing_ok=True)
    try:
        p.parent.rmdir()  # only succeeds when empty
    except OSError:
        pass


def purge_cache() -> None:
    root = cache_dir()
    if not root.is_dir():
        return
    cutoff = time.time() - CACHE_MAX_AGE_S
    for p in root.rglob("*"):
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


def remove_staging(video_path: Path) -> None:
    """Delete a case's local staging folder, but only if it really is one of ours."""
    folder, root = video_path.resolve().parent, Path(settings.storage_dir).resolve()
    if folder.parent == root and folder.name.startswith("VID-"):
        shutil.rmtree(folder, ignore_errors=True)
