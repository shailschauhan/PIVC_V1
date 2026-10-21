import os
import time

os.environ["SQL_SCHEMA"] = "pivc_test"  # its own schema in the Azure SQL database (connection string comes from backend/.env)
os.environ["STORAGE_DIR"] = "./test_storage"

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.analyzers import providers
from app.analyzers.base import Context
from app.analyzers.checks import ANALYZERS
from app.authenticity.background import analyze_background
from app.authenticity.detector import preprocess
from app.config import DEFAULT_RULES, settings
from app import schema
from app.db import T, open_db
from app.decision import decide
from app.main import app
from app.routers import api as api_mod


NO_BG = {"assessed": True, "virtual_suspected": False, "reasons": []}


def detection(fake, faces=30, models=None, background=NO_BG):
    ms = models or {"TimmV2": fake}
    return {"background": background, "ensemble_score": fake, "model_scores": ms, "models": list(ms), "dataset": "dfdc", "device": "cpu",
            "frames_sampled": 32, "frames_with_face": faces, "frames_without_face": 32 - faces,
            "frame_results": [], "per_frame": [fake] * faces}


def vit(hr=72.0, sqi=0.8, secs=30.0, br=15.0, **kw):
    return {"model": "FacePhys.rlap", "hr_bpm": hr, "sqi": sqi, "breathing_rate_bpm": br, "hrv": {}, "duration_s": secs,
            "video": {"fps": 30.0, "frames": 900, "key_frames": 30, "non_key_frames": 870, "no_face_frames": 0,
                      "non_key_fraction": 0.97}, "bvp": [0.0, 1.0], **kw}


def agedet(age=35.0, n=12, spread=2.0, **kw):
    base = {"model": "MiVOLO v2", "age": age, "age_low": None if age is None else age - 2,
            "age_high": None if age is None else age + 2, "spread": spread, "frames_sampled": 16,
            "frames_with_face": n, "frames_aged": n, "multi_face_fraction": 0.0, "frame_results": []}
    return {**base, **kw}


def facedet(decision="match", sim=0.82, **kw):
    return {"decision": decision, "similarity": sim, "reason": "because", "frames_sampled": 12, "frames_with_face": 12, "frames_scored": 12,
            "frames_usable": 12, "low_quality": False, "frames": [], "reference": {"faces_found": 1}, **kw}


def livedet(decision="pass", real=0.97, **kw):
    return {"decision": decision, "real_probability": real, "spoof_suspected": False, "low_quality": False, "reason": "because",
            "frames_sampled": 12, "frames_with_face": 12, "frames_usable": 12, "frames": [], **kw}


def run(det, vitals=None, rules=DEFAULT_RULES, age=None, declared=None, face=None, live=None):
    ctx = Context("R", {}, rules, declared=declared or {})
    ctx.obs = {"authenticity_detail": det, "vitals_detail": vitals or vit(), "age_detail": age or agedet(),
               "face_detail": face or {"skipped": "No ID document photo was provided with this video."},
               "liveness_detail": live or livedet(),
               "authenticity_score": None if det.get("ensemble_score") is None else 1 - det["ensemble_score"]}
    params = {k: fn(ctx) for k, fn in ANALYZERS.items()}
    run.params = params
    return params["video_authenticity"], decide(params, rules)[0]


def test_bands_and_decisions():
    p, d = run(detection(0.02))
    assert p["status"] == "Pass" and d == "Accept"
    p, d = run(detection(0.22))
    assert p["status"] == "Review" and d == "Needs Review"
    p, d = run(detection(0.80))
    assert p["status"] == "Suspected Manipulation" and d == "Reject"


def test_marginal_flag_goes_to_human_not_auto_reject():
    p, d = run(detection(0.31))
    assert p["status"] == "Suspected Manipulation" and p["confidence"] < DEFAULT_RULES["confidence_threshold"]
    assert d == "Needs Review"


def test_virtual_background_blocks_auto_reject_only():
    vb = {"assessed": True, "virtual_suspected": True, "reasons": ["static / replaced background"]}
    p, d = run(detection(0.95, background=vb))  # a very confident flag still goes to a human
    assert p["status"] == "Suspected Manipulation" and d == "Needs Review"
    assert any("human confirmation" in e for e in p["exceptions"])
    p, d = run(detection(0.02, background=vb))  # but it never turns a pass into something else
    assert p["status"] == "Pass" and d == "Accept"


def _frames(kind, n=8, h=240, w=480):
    rng = np.random.default_rng(0)
    still = rng.integers(0, 255, (h, w, 3)).astype(np.float32)
    out = []
    for _ in range(n):
        bg = still.copy() if kind == "pasted" else rng.integers(0, 255, (h, w, 3)).astype(np.float32)
        if kind == "blur":
            import cv2
            bg = cv2.GaussianBlur(bg, (0, 0), 12)
        f = bg.copy()
        f[60:200, 190:290] = rng.integers(0, 255, (140, 100, 3))  # detailed "face"
        out.append((f.astype(np.uint8), (60.0, 190.0, 200.0, 290.0)))
    return out


def test_background_heuristic():
    from app.config import DEFAULT_RULES as R
    assert analyze_background(_frames("real"), R)["virtual_suspected"] is False
    assert "static / replaced background" in analyze_background(_frames("pasted"), R)["reasons"]
    assert "blurred background" in analyze_background(_frames("blur"), R)["reasons"]
    # face fills the frame -> nothing to judge, and nothing is flagged
    big = [(np.zeros((240, 480, 3), np.uint8), (10.0, 20.0, 230.0, 460.0))] * 8
    assert analyze_background(big, R) == {**analyze_background(big, R), "assessed": False, "virtual_suspected": False}


def test_needs_enough_faces_and_reports_errors():
    assert run(detection(0.01, faces=2))[1] == "Needs Review"
    assert run(dict(detection(0.0, faces=0), ensemble_score=None))[1] == "Needs Review"
    p, d = run({"error": "boom"})
    assert p["status"] == "Review" and "boom" in p["exceptions"][0] and d == "Needs Review"


def test_model_disagreement_lowers_confidence():
    agree = run(detection(0.7, models={"A": 0.7, "B": 0.7}))[0]["confidence"]
    split = run(detection(0.4, models={"A": 0.7, "B": 0.1}))[0]["confidence"]
    assert split < agree


def test_preprocess_shape():
    out = preprocess(np.zeros((100, 150, 3), dtype=np.uint8))
    assert out.shape == (3, 224, 224) and out.dtype == np.float32


class FakeDetector:
    """Scores by file content so the whole pipeline can be exercised without model weights."""

    model_names = ["TimmV2"]
    dataset = "dfdc"

    def analyze(self, path):
        content = open(path, "rb").read()
        if content == b"BAD":
            raise ValueError("Video could not be decoded")
        return detection(0.9 if content == b"FAKE" else 0.02)


class FakeVitals:
    """Stands in for open-rppg (loading the real model takes ~40 s)."""

    def analyze(self, path):
        content = open(path, "rb").read()
        if content == b"NOVITALS":
            raise ValueError("rPPG analysis failed: no face")
        if content == b"HIGHHR":
            return vit(hr=140.0)
        if content == b"BLUR":
            return vit(sqi=0.2)
        return vit()


class FakeAge:
    """Stands in for the MiVOLO worker process."""

    def analyze(self, path):
        content = open(path, "rb").read()
        if content == b"NOAGE":
            raise ValueError("Age estimation failed: no face")
        if content == b"OLD":
            return agedet(age=62.0)
        if content == b"WOBBLY":
            return agedet(age=40.0, spread=14.0)
        return agedet()


class FakeServiceClient:
    def __init__(self, ready=True, reason="ok"):
        self.ready, self.reason = ready, reason

    def status(self):
        return {"engine": "fake", "ready": self.ready, "reason": self.reason}


def fake_compare(video, reference, rules):
    """Stands in for the Face Match service; the answer depends on the ID photo's (fake) content."""
    from app import services_client as sc
    ref = open(reference, "rb").read()
    if ref == b"BOOM":
        raise sc.ServiceError("face_match service error 500: boom")
    return {b"OTHER": facedet("mismatch", 0.10), b"BLURRY": facedet("review", 0.35, low_quality=True), b"NOFACE": facedet("no_face", None)}.get(ref, facedet())


def fake_liveness(video, rules):
    from app import services_client as sc
    content = open(video, "rb").read()
    if content == b"LVBOOM":
        raise sc.ServiceError("liveness service unreachable (ConnectError)")
    return {b"SPOOF": livedet("review", 0.08, spoof_suspected=True), b"CLOSEUP": livedet("review", 0.99, low_quality=True),
            b"UNCLEAR": livedet("review", 0.55)}.get(content, livedet())


READY = {"ready": True, "reason": "ok", "models": ["TimmV2"], "dataset": "dfdc"}
AGE_READY = {"engine": "mivolo", "ready": True, "reason": "ok", "model": "MiVOLO v2"}
VITALS_READY = {"engine": "open-rppg", "ready": True, "reason": "ok", "model": "FacePhys.rlap"}


@pytest.fixture
def raw_client(monkeypatch):
    with open_db() as db:
        schema.ensure(db)
        schema.clear_all(db)
    monkeypatch.setattr(api_mod, "engine_status", lambda: READY)
    monkeypatch.setattr(providers, "get_detector", lambda: FakeDetector())
    monkeypatch.setattr(providers, "get_analyzer", lambda: FakeVitals())
    monkeypatch.setattr(api_mod, "get_analyzer", lambda: FakeVitals())
    monkeypatch.setattr(api_mod, "vitals_status", lambda: VITALS_READY)
    monkeypatch.setattr(providers, "get_age_analyzer", lambda: FakeAge())
    monkeypatch.setattr(api_mod, "get_age_analyzer", lambda: FakeAge())
    monkeypatch.setattr(api_mod, "age_status", lambda: AGE_READY)
    from app import services_client as sc
    monkeypatch.setattr(sc, "compare_video", fake_compare)
    monkeypatch.setattr(sc, "check_liveness", fake_liveness)
    monkeypatch.setattr(providers, "get_face_match_client", lambda: FakeServiceClient())
    monkeypatch.setattr(providers, "get_liveness_client", lambda: FakeServiceClient())
    from app import parameters
    monkeypatch.setattr(parameters, "engine_status_fn", lambda spec: (lambda force=False: {"engine": spec["engine"], "ready": True, "reason": "ok"}))
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    monkeypatch.setattr(api_mod, "ensure_preview", lambda p: p)  # test videos are not real media
    from app import storage
    storage._cache.clear()
    with TestClient(app) as c:
        yield c


PASSWORD = "Test-Password-123"


@pytest.fixture
def client(raw_client):
    """A signed-in administrator (username 'admin')."""
    r = raw_client.post("/api/auth/setup", json={"username": "admin", "full_name": "Ada Admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return raw_client


def as_user(client, username, role, password=PASSWORD):
    """Create a user as the admin client and return a separate client signed in as that user."""
    r = client.post("/api/users", json={"username": username, "full_name": username.title(), "role": role, "password": "Temp-Password-1"})
    assert r.status_code == 200, r.text
    other = TestClient(app)
    assert other.post("/api/auth/login", json={"username": username, "password": "Temp-Password-1"}).status_code == 200
    assert other.post("/api/auth/change-password", json={"current_password": "Temp-Password-1", "new_password": password}).status_code == 200
    return other


def vid(name, data=b"x"):
    return ("files", (name, data, "video/mp4"))


def wait_done(c, n, timeout=20):
    from app import jobs
    end = time.time() + timeout
    while time.time() < end:
        if c.get("/api/cases?status=ANALYZED&page_size=200").json()["total"] >= n:
            assert jobs.wait_idle(timeout), "background work (e.g. the blob upload) did not finish"
            return
        time.sleep(0.1)
    raise AssertionError("analysis did not finish")


def test_upload_validation(client):
    assert client.post("/api/cases/upload", files=[("files", ("a.txt", b"x", "text/plain"))]).status_code == 422
    assert client.post("/api/cases/upload", files=[vid("empty.mp4", b"")]).status_code == 422
    assert client.post("/api/cases/upload", files=[vid("big.mp4", b"x" * (2 * 1024 * 1024))]).status_code == 413
    assert client.get("/api/cases").json()["total"] == 0  # nothing partially created


def test_not_ready_blocks_upload(client, monkeypatch):
    monkeypatch.setattr(api_mod, "engine_status", lambda: {"ready": False, "reason": "checkpoints not found"})
    r = client.post("/api/cases/upload", files=[vid("a.mp4")])
    assert r.status_code == 503 and "checkpoints" in r.json()["detail"]
    assert client.get("/api/health").json()["status"] == "degraded"


def test_full_flow(client):
    r = client.post("/api/cases/upload", files=[vid("real.mp4"), vid("fake.mp4", b"FAKE"), vid("bad.mp4", b"BAD")], data={"reference": "P-1"})
    assert len(r.json()["created"]) == 3
    wait_done(client, 3)
    by = {c["filename"]: c for c in client.get("/api/cases").json()["items"]}
    assert by["real.mp4"]["decision"] == "Accept" and by["real.mp4"]["review_status"] == "NOT_REQUIRED"
    assert by["fake.mp4"]["decision"] == "Reject"
    assert by["bad.mp4"]["decision"] == "Needs Review" and by["bad.mp4"]["review_status"] == "PENDING"
    assert by["real.mp4"]["reference"] == "P-1"

    detail = client.get(f"/api/cases/{by['fake.mp4']['id']}").json()
    assert detail["report"]["parameters"]["video_authenticity"]["engine"] == "dfspot"
    assert client.get(f"/api/cases/{by['fake.mp4']['id']}/video").status_code == 200

    cid = by["bad.mp4"]["id"]
    assert client.post(f"/api/cases/{cid}/review", json={"decision": "Maybe"}).status_code == 422
    r = client.post(f"/api/cases/{cid}/review", json={"decision": "Reject", "notes": "x"}, headers={"X-Reviewer": "spoofed"})
    assert r.json()["final_decision"] == "Reject" and r.json()["reviewer"] == "admin"   # the signed-in user, never a header

    d = client.get("/api/dashboard?days=7").json()
    k = d["kpis"]
    assert (k["total"], k["accept"], k["reject"], k["needs_review"], k["reviewed"]) == (3, 1, 1, 1, 1)
    auth = next(t for t in d["tests"] if t["key"] == "video_authenticity")
    assert sum(d["confidence_histogram"]) == 3 and sum(auth["distribution"]["bins"]) == 2
    assert d["top_exceptions"]
    assert client.get("/api/dashboard?days=0").json()["kpis"]["total"] == 3
    assert client.get("/api/cases?parameter=video_authenticity&parameter_status=Suspected Manipulation").json()["total"] == 1
    assert "fake.mp4" in client.get("/api/reports/export.csv").text
    assert client.put("/api/config", json={"bogus": 1}).status_code == 422
    assert client.put("/api/config", json={"confidence_threshold": 0.9}).json()["confidence_threshold"] == 0.9


# ---- blob storage / connections ----------------------------------------------------------------
class FakeBlob:
    """In-memory stand-in for AzureBlobStore (real Azure calls are not exercised by these tests)."""
    container, prefix = "pivc-videos", ""

    def __init__(self, fail=False, fail_delete=False):
        self.blobs, self.fail, self.fail_delete = {}, fail, fail_delete

    def ensure_container(self):
        pass

    def put_file(self, local, key):
        if self.fail:
            raise RuntimeError("AuthorizationFailure")
        self.blobs[key] = open(local, "rb").read()
        return f"blob://{self.container}/{key}"

    def get_file(self, uri, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blobs[uri.split("/", 3)[3]])

    def delete_folder_marker(self, uri):
        folder = uri.split("/", 3)[3].rsplit("/", 1)[0]
        self.markers_removed = getattr(self, "markers_removed", []) + [folder]

    def delete_blob(self, uri):
        if self.fail_delete:
            raise RuntimeError("AuthorizationPermissionMismatch")
        self.blobs.pop(uri.split("/", 3)[3], None)

    def check(self):
        if self.fail:
            raise RuntimeError("AuthorizationFailure")


def sql_rows(table, where="1 = 1", params=()):
    """Rows of a table straight from the database (tests look behind the API with this)."""
    with open_db() as db:
        return db.all(f"SELECT * FROM {getattr(T, table)} WHERE {where}", params, table)


def sql_set(table, id_, **values):
    with open_db() as db:
        row = db.get(table, id_)
        for k, v in values.items():
            setattr(row, k, v)
        db.save(row)
        db.commit()


def case_media(case_id):
    return sql_rows("cases", "id = ?", (case_id,))[0].media


def wait_media(case_id, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        m = case_media(case_id)
        if pred(m):
            return m
        time.sleep(0.1)
    raise AssertionError(f"media never reached expected state: {case_media(case_id)}")


CONN = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=SUPERSECRETKEY==;EndpointSuffix=core.windows.net"
BLOB = {"type": "azure_blob", "name": "Prod blob", "fields": {"auth_mode": "connection_string", "container": "pivc-videos"},
        "secrets": {"connection_string": CONN}}


def add_conn(client, body=BLOB, **kw):
    return client.post("/api/settings/connections", json=body, **kw)


def test_connection_types_describe_the_tabs(client):
    r = client.get("/api/settings/connection-types").json()
    assert [t["type"] for t in r["types"]] == ["azure_blob", "openai", "azure_api", "omni_db", "custom"]
    assert next(t for t in r["types"] if t["type"] == "azure_blob")["used"] is True
    assert all("fields" in t and t["label"] for t in r["types"])


def test_connection_secrets_are_encrypted_and_never_returned(client, monkeypatch):
    from app import crypto
    monkeypatch.setattr(settings, "app_secret_key", "")  # server without APP_SECRET_KEY (ignore backend/.env)
    r = add_conn(client)
    assert r.status_code == 422 and "APP_SECRET_KEY" in r.json()["detail"]

    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    r = add_conn(client)
    assert r.status_code == 200
    c = r.json()
    assert c["active"] is True and c["secrets"]["connection_string"] == {"set": True, "source": "saved"}
    assert c["summary"] == "acct / pivc-videos"
    assert "SUPERSECRETKEY" not in client.get("/api/settings/connections").text
    assert "SUPERSECRETKEY" not in sql_rows("connections")[0].secrets_enc  # encrypted at rest

    # editing without re-entering the secret keeps it
    u = client.put(f"/api/settings/connections/{c['id']}", json={"fields": {"prefix": "pivc/"}}).json()
    assert u["fields"]["prefix"] == "pivc/" and u["secrets"]["connection_string"]["set"] is True
    assert crypto.decrypt_dict(sql_rows("connections")[0].secrets_enc)["connection_string"] == CONN
    # entering a new one replaces it
    client.put(f"/api/settings/connections/{c['id']}", json={"secrets": {"connection_string": "AccountName=other;AccountKey=k"}})
    assert crypto.decrypt_dict(sql_rows("connections")[0].secrets_enc)["connection_string"].startswith("AccountName=other")


def test_connection_validation_and_uniqueness(client, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")

    def attempt(name="Prod blob", secrets=BLOB["secrets"], **fields):
        return add_conn(client, {**BLOB, "name": name, "secrets": secrets, "fields": {**BLOB["fields"], **fields}})

    assert attempt(container="Bad_Name").status_code == 422
    assert attempt(auth_mode="managed_identity").status_code == 422          # needs https account URL
    assert attempt(auth_mode="nonsense").status_code == 422
    assert attempt(name="  ").status_code == 422
    assert attempt(secrets={}).status_code == 422                            # connection string required
    assert attempt().status_code == 200
    assert attempt().status_code == 422                                      # duplicate name
    assert client.post("/api/settings/connections", json={"type": "nope", "name": "x"}).status_code == 422
    api = {"type": "azure_api", "name": "Vision", "fields": {"endpoint": "http://insecure.example"}, "secrets": {"api_key": "k"}}
    assert add_conn(client, api).status_code == 422                          # https only
    api["fields"]["endpoint"] = "https://res.cognitiveservices.azure.com"
    assert add_conn(client, api).status_code == 200


def test_only_one_active_per_type_and_delete_rules(client, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    a = add_conn(client).json()
    b = add_conn(client, {**BLOB, "name": "Second"}).json()
    assert a["active"] and not b["active"]                                    # first becomes active
    assert client.delete(f"/api/settings/connections/{a['id']}").status_code == 409   # active: not deletable
    client.post(f"/api/settings/connections/{b['id']}/activate")
    rows = {r["name"]: r["active"] for r in client.get("/api/settings/connections?type=azure_blob").json()}
    assert rows == {"Prod blob": False, "Second": True}
    assert client.delete(f"/api/settings/connections/{a['id']}").status_code == 200
    assert client.post(f"/api/settings/connections/{b['id']}/deactivate").json()["active"] is False
    assert client.delete("/api/settings/connections/999").status_code == 404


def test_connections_are_admin_only(client, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    reviewer = as_user(client, "rita", "reviewer")
    viewer = as_user(client, "vic", "viewer")
    for who in (reviewer, viewer):
        assert who.get("/api/settings/connections").status_code == 403
        assert who.get("/api/settings/connection-types").status_code == 403
        assert add_conn(who).status_code == 403
        assert who.post("/api/settings/connections/test", json={"type": "custom"}).status_code == 403
    assert add_conn(client).status_code == 200
    assert client.get("/api/settings/connections").status_code == 200


def test_connection_test_endpoint(client, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage, "AzureBlobStore", lambda cfg, conn: FakeBlob())
    body = {"type": "azure_blob", "fields": BLOB["fields"], "secrets": BLOB["secrets"]}
    assert client.post("/api/settings/connections/test", json=body).json()["ok"] is True
    monkeypatch.setattr(storage, "AzureBlobStore", lambda cfg, conn: FakeBlob(fail=True))
    r = client.post("/api/settings/connections/test", json=body).json()
    assert r["ok"] is False and "AuthorizationFailure" in r["message"] and "SUPERSECRETKEY" not in r["message"]
    bad = {"type": "azure_blob", "fields": {**BLOB["fields"], "container": "X"}, "secrets": BLOB["secrets"]}
    assert client.post("/api/settings/connections/test", json=bad).json()["ok"] is False
    # testing a saved connection with edited fields reuses its saved secret
    monkeypatch.setattr(storage, "AzureBlobStore", lambda cfg, conn: FakeBlob() if conn == CONN else FakeBlob(fail=True))
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    cid = add_conn(client).json()["id"]
    assert client.post("/api/settings/connections/test", json={"id": cid, "fields": {"prefix": "x/"}}).json()["ok"] is True


def test_http_probe_refuses_internal_addresses(client):
    for url in ("https://localhost", "https://169.254.169.254", "https://10.0.0.5"):
        r = client.post("/api/settings/connections/test",
                        json={"type": "azure_api", "fields": {"endpoint": url}, "secrets": {"api_key": "k"}}).json()
        assert r["ok"] is False and "private" in r["message"], url


def enable_blob(client, monkeypatch, store):
    from app import storage
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    monkeypatch.setattr(storage, "AzureBlobStore", lambda cfg, conn: store)
    assert add_conn(client).status_code == 200


def test_media_moves_to_blob_and_plays_back(client, monkeypatch):
    store = FakeBlob()
    enable_blob(client, monkeypatch, store)
    r = client.post("/api/cases/upload", files=[vid("real.mp4", b"REALBYTES")])
    cid = r.json()["created"][0]["id"]
    m = wait_media(cid, lambda m: m["video"].startswith("blob://"))
    ref = client.get(f"/api/cases/{cid}").json()["case_ref"]
    assert m["video"] == f"blob://pivc-videos/{ref}/video.mp4"
    assert not os.path.exists(os.path.join(settings.storage_dir, ref))  # staging removed
    assert list(store.blobs.values()) == [b"REALBYTES"]
    resp = client.get(f"/api/cases/{cid}/video")
    assert resp.status_code == 200 and resp.content == b"REALBYTES"
    # re-analysis of a blob-stored case downloads it again and does not duplicate anything
    client.post(f"/api/cases/{cid}/analyze")
    wait_done(client, 1)
    assert client.get(f"/api/cases/{cid}").json()["decision"] == "Accept"
    assert len(store.blobs) == 1


def test_blob_failure_keeps_local_copy(client, monkeypatch):
    enable_blob(client, monkeypatch, FakeBlob(fail=True))
    cid = client.post("/api/cases/upload", files=[vid("real.mp4", b"KEEPME")]).json()["created"][0]["id"]
    m = wait_media(cid, lambda m: "storage_error" in m)
    assert not m["video"].startswith("blob://") and os.path.isfile(m["video"])
    d = client.get(f"/api/cases/{cid}").json()
    assert d["status"] == "ANALYZED" and d["decision"] == "Accept"  # analysis is unaffected
    assert any(a["action"] == "blob storage failed" for a in d["audit"])
    assert client.get(f"/api/cases/{cid}/video").status_code == 200


def test_deactivating_blob_switches_new_uploads_to_local(client, monkeypatch):
    store = FakeBlob()
    enable_blob(client, monkeypatch, store)
    cid = client.get("/api/settings/connections").json()[0]["id"]
    client.post(f"/api/settings/connections/{cid}/deactivate")
    case = client.post("/api/cases/upload", files=[vid("real.mp4", b"LOCAL")]).json()["created"][0]["id"]
    wait_done(client, 1)
    time.sleep(0.5)
    assert not case_media(case)["video"].startswith("blob://") and os.path.isfile(case_media(case)["video"])
    assert store.blobs == {}


# ---- health indicators (rPPG vitals) -------------------------------------------------------------
def health(**kw):
    run(detection(0.02), vit(**kw))
    return run.params["health_indicators"]


def test_vitals_normal_range():
    h = health()
    assert h["status"] == "Normal range" and h["details"]["reliable"] is True and h["confidence"] == 0.8
    assert "medical" in h["details"]["limitations"][0]


def test_vitals_flagged_only_when_reliable():
    h = health(hr=140.0)
    assert h["status"] == "Flagged" and "Heart rate 140" in h["exceptions"][0]
    assert health(hr=40.0)["status"] == "Flagged"
    h = health(br=30.0)
    assert h["status"] == "Flagged" and "Breathing rate" in h["exceptions"][0]
    # same out-of-range value is NOT flagged when the signal is poor or the video too short
    for bad in (dict(hr=140.0, sqi=0.2), dict(hr=140.0, secs=5.0), dict(hr=None), dict(sqi=None)):
        h = health(**bad)
        assert h["status"] == "Low signal quality" and h["exceptions"] == [] and h["details"]["reliable"] is False, bad
    assert "below the minimum" in health(sqi=0.2)["details"]["not_reliable_because"][0]
    assert any("compressed" in r for r in health(sqi=0.2)["details"]["not_reliable_because"])


def test_breathing_plausibility_guard():
    from app.vitals.analyzer import plausible_breathing as pb
    assert pb(0.25) == 15.0 and pb(None) is None
    assert pb(0.0) is None and pb(0.02) is None and pb(2.0) is None     # nonsense from the HRV step is dropped


def test_vitals_unavailable_on_error():
    h = run(detection(0.02), {"error": "model missing"}) and run.params["health_indicators"]
    assert h["status"] == "Unavailable" and h["details"]["error"] == "model missing"


def test_health_never_changes_decision_by_default_and_never_rejects():
    assert run(detection(0.02), vit(hr=150.0))[1] == "Accept"                     # informational by default
    on = {**DEFAULT_RULES, "health_flag_requires_review": True}
    assert run(detection(0.02), vit(hr=150.0), on)[1] == "Needs Review"           # opt-in rule
    assert run(detection(0.02), vit(hr=150.0, sqi=0.1), on)[1] == "Accept"        # unreliable reading ignored
    assert run(detection(0.95), vit(hr=150.0), on)[1] == "Reject"                 # authenticity still decides
    assert run(detection(0.22), vit(), on)[1] == "Needs Review"


def test_vitals_in_pipeline_summary_dashboard_and_csv(client):
    r = client.post("/api/cases/upload", files=[vid("ok.mp4"), vid("hr.mp4", b"HIGHHR"), vid("blur.mp4", b"BLUR")])
    assert len(r.json()["created"]) == 3
    wait_done(client, 3)
    by = {c["filename"]: c for c in client.get("/api/cases").json()["items"]}
    assert (by["ok.mp4"]["vitals"], by["ok.mp4"]["heart_rate"]) == ("Normal range", 72.0)
    assert by["hr.mp4"]["vitals"] == "Flagged" and by["blur.mp4"]["vitals"] == "Low signal quality"
    assert by["hr.mp4"]["decision"] == "Accept"                                     # informational by default
    rep = client.get(f"/api/cases/{by['hr.mp4']['id']}").json()["report"]
    assert rep["parameters"]["health_indicators"]["engine"] == "open-rppg"
    assert rep["labels"]["health_indicators"].startswith("Health")
    assert any("Heart rate 140" in e for e in rep["exception_details"])
    d = client.get("/api/dashboard?days=7").json()
    vt = {o["status"]: o["count"] for o in next(t for t in d["tests"] if t["key"] == "health_indicators")["outcomes"]}
    assert vt == {"Normal range": 1, "Flagged": 1, "Low signal quality": 1, "Unavailable": 0}
    csv = client.get("/api/reports/export.csv").text
    assert "vitals_status" in csv.splitlines()[0] and "Flagged" in csv
    h = client.get("/api/health").json()
    assert h["vitals"]["ready"] is True and h["age"]["ready"] is True


def test_vitals_failure_does_not_affect_authenticity(client):
    client.post("/api/cases/upload", files=[vid("novitals.mp4", b"NOVITALS")])
    wait_done(client, 1)
    c = client.get("/api/cases").json()["items"][0]
    assert c["vitals"] == "Unavailable" and c["status"] == "ANALYZED"
    assert c["authenticity"] == "Pass" and c["decision"] == "Accept"


def test_health_rule_switch_applies_to_new_analyses(client):
    assert client.put("/api/config", json={"health_flag_requires_review": True}).json()["health_flag_requires_review"] is True
    client.post("/api/cases/upload", files=[vid("hr.mp4", b"HIGHHR")])
    wait_done(client, 1)
    c = client.get("/api/cases").json()["items"][0]
    assert c["vitals"] == "Flagged" and c["decision"] == "Needs Review" and c["review_status"] == "PENDING"
    client.put("/api/config", json={"health_flag_requires_review": False})


def one(name, data=b"x"):
    return [("file", (name, data, "video/mp4"))]


def test_standalone_vitals_endpoint(client, monkeypatch):
    r = client.post("/api/vitals/analyze", files=one("a.mp4"))
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "Normal range" and j["details"]["hr_bpm"] == 72.0 and j["filename"] == "a.mp4"
    assert client.post("/api/vitals/analyze", files=one("b.mp4", b"HIGHHR")).json()["status"] == "Flagged"
    assert client.post("/api/vitals/analyze", files=one("c.mp4", b"BLUR")).json()["status"] == "Low signal quality"
    assert client.post("/api/vitals/analyze", files=[("file", ("a.txt", b"x", "text/plain"))]).status_code == 422
    assert client.post("/api/vitals/analyze", files=one("n.mp4", b"NOVITALS")).status_code == 422
    assert client.get("/api/cases").json()["total"] == 0                           # nothing stored
    monkeypatch.setattr(api_mod, "vitals_status", lambda: {"ready": False, "reason": "missing dependency: jax"})
    r = client.post("/api/vitals/analyze", files=one("a.mp4"))
    assert r.status_code == 503 and "jax" in r.json()["detail"]


# ---- age estimation (MiVOLO) ---------------------------------------------------------------------
def age_result(age=35.0, declared=None, rules=DEFAULT_RULES, **kw):
    run(detection(0.02), age=agedet(age=age, **kw), declared=declared, rules=rules)
    return run.params["age_estimation"]


def test_age_without_declared_age_is_informational():
    a = age_result(age=34.0)
    assert a["status"] == "Estimated age" and a["details"]["declared_age"] is None and a["details"]["reliable"] is True
    assert "no declared age" in a["summary"]


def test_age_matches_declared_age_within_tolerance():
    a = age_result(age=38.0, declared={"declared_age": 33})            # +5 <= 7
    assert a["status"] == "Estimated age" and a["details"]["difference"] == 5.0 and a["exceptions"] == []
    assert age_result(age=30.0, declared={"declared_age": 33})["status"] == "Estimated age"


def test_age_mismatch_is_review_and_uses_dob():
    from datetime import date
    a = age_result(age=60.0, declared={"declared_age": 35})
    assert a["status"] == "Review" and "vs declared 35" in a["exceptions"][0] and "+25" in a["exceptions"][0]
    yob = date.today().year - 30
    a = age_result(age=60.0, declared={"dob": f"{yob}-01-01"})
    assert a["details"]["declared_age"] in (29, 30) and a["status"] == "Review"
    assert age_result(age=60.0, declared={"declared_age": "not a number"})["status"] == "Estimated age"  # unparseable -> ignored


def test_age_low_confidence_is_never_a_mismatch():
    a = age_result(age=70.0, declared={"declared_age": 30}, n=3)                 # too few frames
    assert a["status"] == "Low confidence" and a["exceptions"] == [] and a["details"]["reliable"] is False
    a = age_result(age=70.0, declared={"declared_age": 30}, spread=14.0)         # estimates all over the place
    assert a["status"] == "Low confidence" and "vary widely" in a["details"]["not_reliable_because"][0]
    assert age_result(age=None, n=0, spread=None)["status"] == "Low confidence"


def test_age_unavailable_on_error():
    run(detection(0.02), age={"error": "worker down"})
    a = run.params["age_estimation"]
    assert a["status"] == "Unavailable" and a["details"]["error"] == "worker down"


def test_age_mismatch_routes_to_review_but_never_rejects():
    mismatch = dict(age=agedet(age=62.0), declared={"declared_age": 30})
    assert run(detection(0.02), **mismatch)[1] == "Needs Review"
    off = {**DEFAULT_RULES, "age_mismatch_requires_review": False}
    assert run(detection(0.02), rules=off, **mismatch)[1] == "Accept"
    assert run(detection(0.95), **mismatch)[1] == "Reject"                         # authenticity still decides
    assert run(detection(0.02), age=agedet(age=62.0, n=2), declared={"declared_age": 30})[1] == "Accept"  # unreliable ignored


def test_summarize_aggregates_frames():
    from app.age.analyzer import summarize
    rows = [{"n_faces": 1, "age": a} for a in (30, 32, 31, 90, 33)] + [{"n_faces": 2, "age": 31}, {"n_faces": 0}]
    s = summarize(rows)
    assert s["age"] == 31.5 and s["frames_aged"] == 6 and s["frames_with_face"] == 6 and s["frames_sampled"] == 7
    assert abs(s["multi_face_fraction"] - 1 / 7) < 1e-3 and s["age_low"] <= s["age"] <= s["age_high"]
    empty = summarize([{"n_faces": 0}, {"n_faces": 0}])
    assert empty["age"] is None and empty["frames_aged"] == 0


def test_age_in_pipeline_upload_and_summary(client):
    data = {"declared_age": "30"}
    client.post("/api/cases/upload", files=[vid("ok.mp4"), vid("old.mp4", b"OLD"), vid("wob.mp4", b"WOBBLY"), vid("na.mp4", b"NOAGE")], data=data)
    wait_done(client, 4)
    by = {c["filename"]: c for c in client.get("/api/cases").json()["items"]}
    assert (by["ok.mp4"]["age_status"], by["ok.mp4"]["estimated_age"], by["ok.mp4"]["declared_age"]) == ("Estimated age", 35.0, 30)
    assert by["old.mp4"]["age_status"] == "Review" and by["old.mp4"]["decision"] == "Needs Review"
    assert by["old.mp4"]["review_status"] == "PENDING"
    assert by["wob.mp4"]["age_status"] == "Low confidence" and by["wob.mp4"]["decision"] == "Accept"
    assert by["na.mp4"]["age_status"] == "Unavailable" and by["na.mp4"]["decision"] == "Accept" and by["na.mp4"]["status"] == "ANALYZED"
    rep = client.get(f"/api/cases/{by['old.mp4']['id']}").json()["report"]
    assert rep["parameters"]["age_estimation"]["engine"] == "mivolo"
    assert any("vs declared 30" in e for e in rep["exception_details"])
    assert "age_status" in client.get("/api/reports/export.csv").text.splitlines()[0]


def test_declared_details_are_validated(client):
    for bad in ({"declared_age": "abc"}, {"declared_age": "200"}, {"dob": "31-12-1990"}, {"dob": "2999-01-01"}):
        assert client.post("/api/cases/upload", files=[vid("a.mp4")], data=bad).status_code == 422, bad
    assert client.post("/api/cases/upload", files=[vid("a.mp4")], data={"dob": "1990-05-01"}).status_code == 200
    wait_done(client, 1)  # let the background analysis finish while the test doubles are still in place


def test_standalone_age_endpoint(client, monkeypatch):
    r = client.post("/api/age/analyze", files=one("a.mp4"), data={"declared_age": "34"})
    assert r.status_code == 200 and r.json()["status"] == "Estimated age" and r.json()["details"]["age"] == 35.0
    assert client.post("/api/age/analyze", files=one("o.mp4", b"OLD"), data={"declared_age": "30"}).json()["status"] == "Review"
    assert client.post("/api/age/analyze", files=one("n.mp4", b"NOAGE")).status_code == 422
    assert client.post("/api/age/analyze", files=[("file", ("a.txt", b"x", "text/plain"))]).status_code == 422
    assert client.post("/api/age/analyze", files=one("a.mp4"), data={"declared_age": "x"}).status_code == 422
    assert client.get("/api/cases").json()["total"] == 0
    monkeypatch.setattr(api_mod, "age_status", lambda: {"ready": False, "reason": "not found: models/age"})
    r = client.post("/api/age/analyze", files=one("a.mp4"))
    assert r.status_code == 503 and "models/age" in r.json()["detail"]


def test_dashboard_is_generated_from_the_test_registry(client):
    from app import parameters
    client.post("/api/cases/upload", files=[vid("a.mp4"), vid("o.mp4", b"OLD")], data={"declared_age": "30"})
    wait_done(client, 2)
    d = client.get("/api/dashboard?days=7").json()
    assert [t["key"] for t in d["tests"]] == parameters.keys() == ["video_authenticity", "health_indicators", "age_estimation", "face_match", "liveness"]
    age = next(t for t in d["tests"] if t["key"] == "age_estimation")
    assert age["total"] == 2 and {o["status"]: o["count"] for o in age["outcomes"]}["Review"] == 1
    assert (age["clean"], age["attention"], age["unassessed"]) == (1, 1, 0) and age["attention_rate"] == 0.5
    assert sum(age["distribution"]["bins"]) == 2 and age["distribution"]["bins"][3] == 1 and age["distribution"]["bins"][6] == 1
    auth = next(t for t in d["tests"] if t["key"] == "video_authenticity")
    assert [m["label"] for m in auth["distribution"]["markers"]] == ["review", "flag"] and auth["distribution"]["markers"][1]["at"] == 0.3
    assert {e["key"] for e in d["engines"]} == set(parameters.keys())


# ---- bulk delete / re-run ------------------------------------------------------------------------
def upload_ids(client, *names_data):
    r = client.post("/api/cases/upload", files=[vid(n, d) for n, d in names_data])
    return [c["id"] for c in r.json()["created"]]


def bulk(client, action, ids, **kw):
    return client.post("/api/cases/bulk", json={"action": action, "ids": ids}, **kw)


def test_bulk_validation(client):
    assert bulk(client, "delete", []).status_code == 422
    assert bulk(client, "explode", [1]).status_code == 422
    assert bulk(client, "delete", list(range(1, 502))).status_code == 422
    r = bulk(client, "delete", [999]).json()
    assert r["failed"] == 1 and r["results"][0]["message"] == "Not found"


def test_bulk_rerun_reanalyses_and_keeps_the_previous_review_in_the_audit(client):
    alice, bob = as_user(client, "alice", "reviewer"), as_user(client, "bob", "reviewer")
    a, b = upload_ids(client, ("a.mp4", b"x"), ("b.mp4", b"FAKE"))
    wait_done(client, 2)
    assert alice.post(f"/api/cases/{b}/review", json={"decision": "Accept", "notes": "checked"}).status_code == 200
    r = bulk(bob, "rerun", [a, b, a]).json()                                          # duplicate id is ignored
    assert r["succeeded"] == 2 and r["failed"] == 0 and len(r["results"]) == 2
    wait_done(client, 2)
    d = client.get(f"/api/cases/{b}").json()
    assert d["status"] == "ANALYZED" and d["decision"] == "Reject"
    ev = next(x for x in d["audit"] if x["action"] == "re-run requested")
    assert ev["actor"] == "bob" and ev["detail"]["final_decision"] == "Accept" and ev["detail"]["reviewer"] == "alice"


def test_bulk_rerun_is_blocked_when_models_are_not_ready(client, monkeypatch):
    (a,) = upload_ids(client, ("a.mp4", b"x"))
    wait_done(client, 1)
    monkeypatch.setattr(api_mod, "engine_status", lambda: {"ready": False, "reason": "checkpoints not found"})
    assert bulk(client, "rerun", [a]).status_code == 503


def test_bulk_delete_removes_everything_for_local_videos(client):
    a, b, c = upload_ids(client, ("a.mp4", b"x"), ("b.mp4", b"FAKE"), ("c.mp4", b"x"))
    wait_done(client, 3)
    folders = [os.path.dirname(case_media(i)["video"]) for i in (a, b)]
    refs = {i: client.get(f"/api/cases/{i}").json()["case_ref"] for i in (a, b)}
    carol = as_user(client, "carol", "admin")
    r = bulk(carol, "delete", [a, b, 12345]).json()
    assert (r["succeeded"], r["failed"]) == (2, 1)
    assert not any(os.path.exists(f) for f in folders)                                   # files gone
    assert client.get(f"/api/cases/{a}").status_code == 404 and client.get(f"/api/cases/{c}").status_code == 200
    assert {x.id for x in sql_rows("cases")} == {c}
    for table in ("audit_log", "case_params", "case_exceptions"):                        # no orphan rows
        assert {x.case_id for x in sql_rows(table)} <= {c}
    log = {x.case_ref: x for x in sql_rows("deletion_log")}
    assert set(log) == set(refs.values()) and log[refs[b]].deleted_by == "carol" and log[refs[b]].decision == "Reject"
    assert log[refs[a]].filename == "a.mp4" and log[refs[a]].stored_in == "local"
    assert client.get("/api/dashboard?days=7").json()["kpis"]["total"] == 1


def test_bulk_delete_skips_videos_being_analysed(client):
    (a,) = upload_ids(client, ("a.mp4", b"x"))
    wait_done(client, 1)
    sql_set("cases", a, status="PROCESSING")
    for action in ("delete", "rerun"):
        r = bulk(client, action, [a]).json()
        assert r["failed"] == 1 and "analysed right now" in r["results"][0]["message"]
    assert client.get(f"/api/cases/{a}").status_code == 200


def test_bulk_delete_removes_blobs_and_cache(client, monkeypatch):
    store = FakeBlob()
    enable_blob(client, monkeypatch, store)
    a, b = upload_ids(client, ("a.mp4", b"AAA"), ("b.mp4", b"BBB"))
    for i in (a, b):
        wait_media(i, lambda m: m["video"].startswith("blob://"))
    assert len(store.blobs) >= 2
    client.get(f"/api/cases/{a}/video")                                                  # fills the local cache
    r = bulk(client, "delete", [a])
    assert r.json()["succeeded"] == 1
    assert len(store.markers_removed) == 1 and store.markers_removed[0].startswith("VID-")   # empty case folder cleaned up
    remaining = list(store.blobs.values())
    assert b"AAA" not in remaining and b"BBB" in remaining                               # only a's blobs were deleted
    assert sql_rows("deletion_log")[0].stored_in == "blob"


def test_bulk_delete_keeps_the_case_when_blob_deletion_fails(client, monkeypatch):
    store = FakeBlob()
    enable_blob(client, monkeypatch, store)
    (a,) = upload_ids(client, ("a.mp4", b"AAA"))
    wait_media(a, lambda m: m["video"].startswith("blob://"))
    store.fail_delete = True
    r = bulk(client, "delete", [a]).json()
    assert r["failed"] == 1 and "AuthorizationPermissionMismatch" in r["results"][0]["message"]
    assert client.get(f"/api/cases/{a}").status_code == 200                              # nothing silently orphaned
    store.fail_delete = False
    assert bulk(client, "delete", [a]).json()["succeeded"] == 1 and b"AAA" not in store.blobs.values()


def test_bulk_delete_needs_the_admin_role_but_rerun_does_not(client):
    (a,) = upload_ids(client, ("a.mp4", b"x"))
    wait_done(client, 1)
    reviewer, viewer = as_user(client, "rita", "reviewer"), as_user(client, "vic", "viewer")
    assert bulk(reviewer, "delete", [a]).status_code == 403
    assert bulk(viewer, "delete", [a]).status_code == 403 and bulk(viewer, "rerun", [a]).status_code == 403
    assert client.get(f"/api/cases/{a}").status_code == 200
    assert bulk(reviewer, "rerun", [a]).status_code == 200
    wait_done(client, 1)
    assert bulk(client, "delete", [a]).json()["succeeded"] == 1


# ---- sign-in, roles and user management ----------------------------------------------------------
def test_first_run_setup(raw_client):
    c = raw_client
    assert c.get("/api/auth/status").json() == {"setup_required": True}
    assert c.get("/api/cases").status_code == 401
    for bad in ({"username": "ab", "password": PASSWORD}, {"username": "bad name", "password": PASSWORD},
                {"username": "admin", "password": "short"}, {"username": "admin", "password": "admin-admin-admin"},
                {"username": "admin", "password": "aaaaaaaaaaaaaaaa"}):
        assert c.post("/api/auth/setup", json=bad).status_code == 422, bad
    r = c.post("/api/auth/setup", json={"username": "Admin", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["username"] == "admin"   # lower-cased
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert c.get("/api/auth/me").json()["username"] == "admin" and c.get("/api/cases").status_code == 200
    assert c.get("/api/auth/status").json() == {"setup_required": False}
    assert TestClient(app).post("/api/auth/setup", json={"username": "evil", "password": PASSWORD}).status_code == 409


def test_everything_except_health_and_auth_needs_a_login(client):
    anon = TestClient(app)
    assert anon.get("/api/health").status_code == 200 and anon.get("/api/auth/status").status_code == 200
    (a,) = upload_ids(client, ("a.mp4", b"x"))
    wait_done(client, 1)
    for method, path in [("get", "/api/cases"), ("get", f"/api/cases/{a}"), ("get", f"/api/cases/{a}/video"), ("get", "/api/dashboard"),
                         ("get", "/api/config"), ("get", "/api/reports/export.csv"), ("get", "/api/users"), ("get", "/api/auth/me"),
                         ("post", "/api/cases/upload"), ("post", "/api/cases/bulk"), ("put", "/api/config"),
                         ("get", "/api/settings/connections")]:
        assert getattr(anon, method)(path).status_code == 401, (method, path)


def test_login_logout_and_generic_errors(client):
    other = TestClient(app)
    unknown = other.post("/api/auth/login", json={"username": "nobody", "password": PASSWORD})
    wrong = other.post("/api/auth/login", json={"username": "admin", "password": "wrong-password-1"})
    assert unknown.status_code == wrong.status_code == 401 and unknown.json() == wrong.json()   # no user enumeration
    assert other.post("/api/auth/login", json={"username": "ADMIN", "password": PASSWORD}).status_code == 200
    assert other.get("/api/cases").status_code == 200
    assert other.post("/api/auth/logout").status_code == 200
    assert other.get("/api/cases").status_code == 401 and other.get("/api/auth/me").status_code == 401


def test_account_locks_after_repeated_failures_and_admin_can_unlock(client):
    victim = as_user(client, "vera", "reviewer")
    other = TestClient(app)
    for _ in range(5):
        assert other.post("/api/auth/login", json={"username": "vera", "password": "wrong-password-1"}).status_code == 401
    assert other.post("/api/auth/login", json={"username": "vera", "password": PASSWORD}).status_code == 423   # locked even for the right password
    uid = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "vera")
    assert next(u for u in client.get("/api/users").json() if u["id"] == uid)["locked"] is True
    assert client.post(f"/api/users/{uid}/unlock").status_code == 200
    assert other.post("/api/auth/login", json={"username": "vera", "password": PASSWORD}).status_code == 200


def test_role_permissions(client):
    (a,) = upload_ids(client, ("a.mp4", b"x"))
    wait_done(client, 1)
    viewer, reviewer = as_user(client, "vic", "viewer"), as_user(client, "rita", "reviewer")
    files = [("files", ("n.mp4", b"x", "video/mp4"))]
    # viewer: read only
    for path in ("/api/cases", f"/api/cases/{a}", f"/api/cases/{a}/video", "/api/dashboard", "/api/config", "/api/reports/export.csv"):
        assert viewer.get(path).status_code == 200, path
    assert viewer.post("/api/cases/upload", files=files).status_code == 403
    assert viewer.post(f"/api/cases/{a}/review", json={"decision": "Accept"}).status_code == 403
    assert viewer.post(f"/api/cases/{a}/analyze").status_code == 403
    assert viewer.post("/api/vitals/analyze", files=[("file", ("n.mp4", b"x", "video/mp4"))]).status_code == 403
    # reviewer: can work cases, cannot administer
    assert reviewer.post("/api/cases/upload", files=files).status_code == 200
    assert reviewer.post(f"/api/cases/{a}/review", json={"decision": "Accept"}).status_code == 200
    assert reviewer.put("/api/config", json={"confidence_threshold": 0.8}).status_code == 403
    assert reviewer.get("/api/users").status_code == 403 and reviewer.post("/api/users", json={}).status_code == 403
    assert viewer.put("/api/config", json={"confidence_threshold": 0.8}).status_code == 403
    # admin: everything
    assert client.put("/api/config", json={"confidence_threshold": 0.8}).status_code == 200


def test_user_management_rules(client):
    t = {"username": "newbie", "full_name": "New Bie", "role": "reviewer", "password": "Temp-Password-1"}
    assert client.post("/api/users", json={**t, "role": "root"}).status_code == 422
    assert client.post("/api/users", json={**t, "password": "short"}).status_code == 422
    assert client.post("/api/users", json={**t, "password": "newbie-newbie-1"}).status_code == 422     # contains the username
    assert client.post("/api/users", json={**t, "username": "x"}).status_code == 422
    u = client.post("/api/users", json=t).json()
    assert u["role"] == "reviewer" and u["must_change_password"] is True and "password" not in str(u).lower().replace("must_change_password", "")
    assert client.post("/api/users", json={**t, "username": "NewBie"}).status_code == 409            # case-insensitive unique

    # first sign-in forces a password change before anything else works
    n = TestClient(app)
    assert n.post("/api/auth/login", json={"username": "newbie", "password": "Temp-Password-1"}).status_code == 200
    assert n.get("/api/auth/me").json()["must_change_password"] is True
    forced = n.get("/api/cases")
    assert forced.status_code == 403 and "change your password" in forced.json()["detail"]
    assert n.post("/api/auth/change-password", json={"current_password": "nope", "new_password": PASSWORD}).status_code == 403
    assert n.post("/api/auth/change-password", json={"current_password": "Temp-Password-1", "new_password": "Temp-Password-1"}).status_code == 422
    assert n.post("/api/auth/change-password", json={"current_password": "Temp-Password-1", "new_password": PASSWORD}).status_code == 200
    assert n.get("/api/cases").status_code == 200

    # changing the password signs out the user's other sessions
    other_device = TestClient(app)
    other_device.post("/api/auth/login", json={"username": "newbie", "password": PASSWORD})
    assert n.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "Another-Password-9"}).status_code == 200
    assert other_device.get("/api/cases").status_code == 401 and n.get("/api/cases").status_code == 200

    # edit, deactivate (ends sessions, blocks sign-in), reactivate, reset password, delete
    assert client.put(f"/api/users/{u['id']}", json={"role": "viewer", "full_name": "Nina"}).json()["role"] == "viewer"
    assert n.post("/api/cases/upload", files=[("files", ("n.mp4", b"x", "video/mp4"))]).status_code == 403       # new role applies at once
    assert client.put(f"/api/users/{u['id']}", json={"active": False}).json()["active"] is False
    assert n.get("/api/cases").status_code == 401
    assert TestClient(app).post("/api/auth/login", json={"username": "newbie", "password": "Another-Password-9"}).status_code == 403
    client.put(f"/api/users/{u['id']}", json={"active": True})
    assert client.post(f"/api/users/{u['id']}/reset-password", json={"password": "Reset-Password-7"}).json()["must_change_password"] is True
    assert TestClient(app).post("/api/auth/login", json={"username": "newbie", "password": "Another-Password-9"}).status_code == 401
    assert TestClient(app).post("/api/auth/login", json={"username": "newbie", "password": "Reset-Password-7"}).status_code == 200
    assert client.delete(f"/api/users/{u['id']}").status_code == 200
    assert TestClient(app).post("/api/auth/login", json={"username": "newbie", "password": "Reset-Password-7"}).status_code == 401
    assert client.delete("/api/users/9999").status_code == 404


def test_there_is_always_an_active_admin_and_you_cannot_lock_yourself_out(client):
    me = next(u for u in client.get("/api/users").json() if u["username"] == "admin")
    for body in ({"role": "viewer"}, {"active": False}):
        assert client.put(f"/api/users/{me['id']}", json=body).status_code == 409
    assert client.delete(f"/api/users/{me['id']}").status_code == 409
    second = as_user(client, "second", "admin")
    sid = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "second")
    assert client.put(f"/api/users/{sid}", json={"role": "reviewer"}).status_code == 200             # fine: another admin remains
    assert second.get("/api/users").status_code == 403
    client.put(f"/api/users/{sid}", json={"role": "admin"})
    assert second.put(f"/api/users/{me['id']}", json={"active": False}).status_code == 200            # now 'second' may deactivate 'admin'
    assert second.put(f"/api/users/{sid}", json={"role": "viewer"}).status_code == 409                # but nobody may remove the last admin


def test_expired_sessions_are_rejected(client):
    from datetime import datetime, timedelta, timezone
    assert client.get("/api/cases").status_code == 200
    with open_db() as db:
        db.run(f"UPDATE {T.user_sessions} SET expires_at = ?", (datetime.now(timezone.utc) - timedelta(minutes=1),))
        db.commit()
    assert client.get("/api/cases").status_code == 401


def test_bootstrap_admin_from_environment(raw_client, monkeypatch):
    from app import auth
    monkeypatch.setattr(settings, "admin_username", "Boss")
    monkeypatch.setattr(settings, "admin_password", PASSWORD)
    with open_db() as db:
        auth.bootstrap_admin(db)
        auth.bootstrap_admin(db)                     # idempotent
    (u,) = sql_rows("users")
    assert u.username == "boss" and u.role == "admin" and u.must_change_password is False and u.password_hash.startswith("scrypt$")
    assert PASSWORD not in u.password_hash
    assert raw_client.get("/api/auth/status").json() == {"setup_required": False}
    assert raw_client.post("/api/auth/login", json={"username": "boss", "password": PASSWORD}).status_code == 200


def test_password_hashing():
    from app.auth import hash_password, verify_password
    h1, h2 = hash_password("Correct-Horse-1"), hash_password("Correct-Horse-1")
    assert h1 != h2 and verify_password("Correct-Horse-1", h1) and not verify_password("correct-horse-1", h1)
    assert not verify_password("x", "garbage") and not verify_password("x", "")


# ---- face match and liveness (standalone services) -----------------------------------------------
def face_result(face=None, **kw):
    run(detection(0.02), face=face, **kw)
    return run.params["face_match"]


def live_result(live=None, **kw):
    run(detection(0.02), live=live, **kw)
    return run.params["liveness"]


def test_face_match_statuses_from_the_service_decision():
    m = face_result(facedet("match", 0.82))
    assert m["status"] == "Match" and m["exceptions"] == [] and m["confidence"] > 0.8 and m["details"]["limitations"]
    mm = face_result(facedet("mismatch", 0.08))
    assert mm["status"] == "Mismatch" and "does not match" in mm["exceptions"][0] and mm["confidence"] > 0.6
    r = face_result(facedet("review", 0.35))
    assert r["status"] == "Review" and "inconclusive" in r["exceptions"][0]
    nf = face_result(facedet("no_face", None))
    assert nf["status"] == "Review" and "not possible" in nf["exceptions"][0]
    assert face_result(None)["status"] == "Not checked" and face_result(None)["exceptions"] == []
    u = face_result({"error": "Face Match service is not available: not configured"})
    assert u["status"] == "Unavailable" and "not configured" in u["details"]["error"]


def test_face_match_routes_to_review_but_never_rejects():
    assert run(detection(0.02), face=facedet("match"))[1] == "Accept"
    assert run(detection(0.02), face=facedet("mismatch", 0.05))[1] == "Needs Review"
    assert run(detection(0.02), face=facedet("review", 0.35))[1] == "Needs Review"
    assert run(detection(0.02), face=None)[1] == "Accept"                                    # no ID photo: not a reason to review
    assert run(detection(0.02), face={"error": "x"})[1] == "Accept"                          # service down: reported, not a reason to review
    off = {**DEFAULT_RULES, "face_mismatch_requires_review": False}
    assert run(detection(0.02), rules=off, face=facedet("mismatch", 0.05))[1] == "Accept"
    assert run(detection(0.95), face=facedet("mismatch", 0.05))[1] == "Reject"               # authenticity alone decides Reject


def test_liveness_statuses():
    assert live_result(livedet())["status"] == "Pass" and "risk signal" in live_result(livedet())["summary"]
    s = live_result(livedet("review", 0.08, spoof_suspected=True))
    assert s["status"] == "Review" and "presentation attack" in s["exceptions"][0] and s["confidence"] > 0.9
    u = live_result(livedet("review", 0.55))
    assert u["status"] == "Review" and "unclear" in u["exceptions"][0]
    lq = live_result(livedet("review", 0.99, low_quality=True))
    assert lq["status"] == "Low confidence" and lq["exceptions"] == [] and lq["confidence"] <= 0.5       # a high score is not trusted
    assert live_result(livedet("no_face", None))["status"] == "Low confidence"
    assert live_result({"error": "down"})["status"] == "Unavailable"


def test_only_a_suspected_attack_routes_liveness_to_review():
    assert run(detection(0.02), live=livedet("review", 0.08, spoof_suspected=True))[1] == "Needs Review"
    assert run(detection(0.02), live=livedet("review", 0.55))[1] == "Accept"                 # unclear: reported only
    assert run(detection(0.02), live=livedet("review", 0.99, low_quality=True))[1] == "Accept"
    off = {**DEFAULT_RULES, "liveness_suspected_requires_review": False}
    assert run(detection(0.02), rules=off, live=livedet("review", 0.08, spoof_suspected=True))[1] == "Accept"
    assert run(detection(0.95), live=livedet("review", 0.08, spoof_suspected=True))[1] == "Reject"


# ---------- the service client (HTTP) ----------
def test_service_client_status_and_errors(monkeypatch):
    import httpx
    from app import services_client as sc
    mk = lambda **kw: sc.ServiceClient("face_match", "SFace", kw.get("url", "http://svc"), "k", kw.get("enabled", True), 5)  # noqa: E731
    assert "disabled" in mk(enabled=False).status()["reason"]
    assert "not configured" in mk(url="").status()["reason"]

    def get(url, headers=None, timeout=None):
        if url.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200 if headers.get("X-API-Key") == "k" else 401, json={})
    monkeypatch.setattr(httpx, "get", get)
    st = mk().status()
    assert {k: st[k] for k in ("engine", "ready", "reason", "url")} == {"engine": "SFace", "ready": True, "reason": "ok", "url": "http://svc"}
    assert st["info"] == {} and isinstance(st["latency_ms"], int)
    bad = sc.ServiceClient("face_match", "SFace", "http://svc", "wrong", True, 5)
    assert "rejected the API key" in bad.status()["reason"]

    def down(url, headers=None, timeout=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", down)
    assert "unreachable" in mk().status()["reason"]

    # POST: success, readable error detail, network failure
    f = __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "a.bin"
    f.write_bytes(b"x")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"decision": "match"}))
    assert mk().post("/v1/x", {"video": f}, {}) == {"decision": "match"}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(422, json={"detail": "frames must be between 1 and 60."}))
    with pytest.raises(sc.ServiceError, match="422.*frames must be"):
        mk().post("/v1/x", {"video": f}, {})
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    with pytest.raises(sc.ServiceError, match="unreachable"):
        mk().post("/v1/x", {"video": f}, {})


def test_status_is_cached_briefly(monkeypatch):
    import httpx
    from app import services_client as sc
    calls = []
    monkeypatch.setattr(httpx, "get", lambda url, **k: (calls.append(url), httpx.Response(200, json={}))[1])
    c = sc.ServiceClient("liveness", "MiniFASNet", "http://svc", "k", True, 5)
    c.status(); c.status(); c.status()
    assert len(calls) == 2                                            # /health + /v1/info once, then cached


# ---------- upload with an ID document ----------
def upload_with_id(client, name, content, id_bytes, id_name="id.jpg", **data):
    files = [("files", (name, content, "video/mp4"))]
    if id_bytes is not None:
        files.append(("id_document", (id_name, id_bytes, "image/jpeg")))
    return client.post("/api/cases/upload", files=files, data=data)


def test_upload_with_id_document_runs_face_match_and_liveness(client):
    r = upload_with_id(client, "ok.mp4", b"x", b"SAME")
    assert r.status_code == 200 and r.json()["created"][0]["has_id_document"] is True
    r2 = upload_with_id(client, "other.mp4", b"x", b"OTHER")
    r3 = upload_with_id(client, "none.mp4", b"x", None)
    r4 = upload_with_id(client, "spoof.mp4", b"SPOOF", b"SAME")
    r5 = upload_with_id(client, "down.mp4", b"LVBOOM", b"BOOM")
    wait_done(client, 5)
    by = {c["filename"]: c for c in client.get("/api/cases").json()["items"]}
    assert (by["ok.mp4"]["face_status"], by["ok.mp4"]["liveness_status"], by["ok.mp4"]["decision"]) == ("Match", "Pass", "Accept")
    assert by["ok.mp4"]["similarity"] == 0.82 and by["ok.mp4"]["real_probability"] == 0.97
    assert by["other.mp4"]["face_status"] == "Mismatch" and by["other.mp4"]["decision"] == "Needs Review" and by["other.mp4"]["review_status"] == "PENDING"
    assert by["none.mp4"]["face_status"] == "Not checked" and by["none.mp4"]["has_id_document"] is False and by["none.mp4"]["decision"] == "Accept"
    assert by["spoof.mp4"]["liveness_status"] == "Review" and by["spoof.mp4"]["decision"] == "Needs Review"
    assert by["down.mp4"]["face_status"] == "Unavailable" and by["down.mp4"]["liveness_status"] == "Unavailable"
    assert by["down.mp4"]["status"] == "ANALYZED" and by["down.mp4"]["authenticity"] == "Pass"      # other tests unaffected
    rep = client.get(f"/api/cases/{by['other.mp4']['id']}").json()["report"]
    assert rep["parameters"]["face_match"]["engine"] == "sface" and rep["parameters"]["liveness"]["engine"] == "minifasnet"
    assert any("does not match" in e for e in rep["exception_details"])
    csv = client.get("/api/reports/export.csv").text.splitlines()
    assert "face_match_status" in csv[0] and "liveness_real_probability" in csv[0]


def test_unavailable_services_are_reported_not_fatal(client, monkeypatch):
    monkeypatch.setattr(providers, "get_face_match_client", lambda: FakeServiceClient(False, "not configured (set FACE_MATCH_URL)"))
    monkeypatch.setattr(providers, "get_liveness_client", lambda: FakeServiceClient(False, "service unreachable at http://x"))
    upload_with_id(client, "a.mp4", b"x", b"SAME")
    wait_done(client, 1)
    c = client.get("/api/cases").json()["items"][0]
    assert c["face_status"] == "Unavailable" and c["liveness_status"] == "Unavailable" and c["decision"] == "Accept"
    d = client.get(f"/api/cases/{c['id']}").json()["report"]["parameters"]
    assert "not configured" in d["face_match"]["details"]["error"] and "unreachable" in d["liveness"]["details"]["error"]


def test_id_document_validation(client):
    assert upload_with_id(client, "a.mp4", b"x", b"d", id_name="id.pdf").status_code == 422
    assert upload_with_id(client, "a.mp4", b"x", b"", id_name="id.jpg").status_code == 422
    big = b"x" * (11 * 1024 * 1024)
    assert upload_with_id(client, "a.mp4", b"x", big).status_code == 413
    assert client.get("/api/cases").json()["total"] == 0                                   # nothing half-created


def test_id_document_is_viewable_only_by_reviewers_and_admins(client):
    cid = upload_with_id(client, "a.mp4", b"x", b"SAME").json()["created"][0]["id"]
    cid_no = upload_with_id(client, "b.mp4", b"x", None).json()["created"][0]["id"]
    wait_done(client, 2)
    viewer, reviewer = as_user(client, "vic", "viewer"), as_user(client, "rita", "reviewer")
    assert viewer.get(f"/api/cases/{cid}/id-document").status_code == 403
    r = reviewer.get(f"/api/cases/{cid}/id-document")
    assert r.status_code == 200 and r.content == b"SAME" and "no-store" in r.headers["cache-control"]
    assert client.get(f"/api/cases/{cid_no}/id-document").status_code == 404
    assert TestClient(app).get(f"/api/cases/{cid}/id-document").status_code == 401


def test_deleting_a_case_removes_the_id_document_file(client):
    cid = upload_with_id(client, "a.mp4", b"x", b"SAME").json()["created"][0]["id"]
    wait_done(client, 1)
    folder = os.path.dirname(case_media(cid)["video"])
    assert os.path.isfile(os.path.join(folder, "id_document.jpg"))
    assert bulk(client, "delete", [cid]).json()["succeeded"] == 1
    assert not os.path.exists(folder)


def test_id_document_moves_to_blob_storage_with_the_video_and_is_deleted_with_it(client, monkeypatch):
    store = FakeBlob()
    enable_blob(client, monkeypatch, store)
    cid = upload_with_id(client, "a.mp4", b"VIDEO", b"SAME").json()["created"][0]["id"]
    m = wait_media(cid, lambda m: m["video"].startswith("blob://") and str(m.get("id_document", "")).startswith("blob://"))
    ref = client.get(f"/api/cases/{cid}").json()["case_ref"]
    assert m["id_document"] == f"blob://pivc-videos/{ref}/id_document.jpg"
    assert store.blobs[f"{ref}/id_document.jpg"] == b"SAME"
    assert not os.path.exists(os.path.join(settings.storage_dir, ref))                     # nothing left on local disk
    assert client.get(f"/api/cases/{cid}/id-document").content == b"SAME"                  # served back from blob storage
    client.post(f"/api/cases/{cid}/analyze")                                               # re-run downloads it again
    wait_done(client, 1)
    assert client.get(f"/api/cases").json()["items"][0]["face_status"] == "Match"
    r = bulk(client, "delete", [cid]).json()
    assert r["succeeded"] == 1, r
    assert all("id_document" not in k for k in store.blobs) and not store.blobs            # video, preview and ID photo all gone


# ---- dashboard numbers ---------------------------------------------------------------------------
def seed_case(ref, created_ago_h, decision="Accept", review="NOT_REQUIRED", final=None, analyzed_after_s=30, reviewed_after_h=None,
              params=None, exceptions=(), status="ANALYZED", confidence=0.9):
    """Insert a finished case directly so every dashboard figure has a known expected value."""
    from datetime import datetime, timedelta, timezone
    created = datetime.now(timezone.utc) - timedelta(hours=created_ago_h)
    values = dict(case_ref=ref, proposal_no="-", customer_id="-", customer_name=f"{ref}.mp4", source="upload", declared={},
                  media={"original_name": f"{ref}.mp4"}, created_at=created, status=status)
    if status == "ANALYZED":
        analyzed = created + timedelta(seconds=analyzed_after_s)
        values.update(decision=decision, confidence=confidence, processing_ms=20_000, analyzed_at=analyzed, review_status=review,
                      final_decision=final if review == "COMPLETED" else (None if review == "PENDING" else decision),
                      report={"parameters": {}, "exception_details": [], "labels": {}, "rules_applied": {}})
        if review == "COMPLETED":
            values.update(reviewer="alice", reviewed_at=analyzed + timedelta(hours=reviewed_after_h or 1))
    with open_db() as db:
        c = db.insert("cases", **values)
        if status == "ANALYZED":
            for key, st, score in (params or []):
                db.insert("case_params", case_id=c.id, parameter=key, status=st, confidence=0.8, score=score, engine="x")
            for key, reason in exceptions:
                db.insert("case_exceptions", case_id=c.id, parameter=key, reason=reason, message=reason)
        db.commit()
        return c.id


def dash(client, days=7):
    return client.get(f"/api/dashboard?days={days}").json()


def test_timestamps_are_returned_as_utc_with_an_offset(client):
    seed_case("T1", 2)
    from datetime import datetime
    row = client.get("/api/cases").json()["items"][0]
    for k in ("created_at", "analyzed_at"):
        dt = datetime.fromisoformat(row[k])
        assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0, f"{k} lost its timezone: {row[k]}"
    assert datetime.fromisoformat(dash(client)["generated_at"]).tzinfo is not None


def test_kpis_funnel_and_review_figures(client):
    # 3 auto-accepted, 1 auto-rejected, 4 sent to review (2 decided by a person, 2 still waiting), 1 failed, 1 overturned auto-decision
    for i in range(3):
        seed_case(f"A{i}", 5)
    seed_case("R0", 6, decision="Reject")
    seed_case("N1", 8, "Needs Review", "COMPLETED", final="Accept", reviewed_after_h=2)
    seed_case("N2", 9, "Needs Review", "COMPLETED", final="Reject", reviewed_after_h=6)
    seed_case("N3", 10, "Needs Review", "PENDING")
    seed_case("N4", 30, "Needs Review", "PENDING")                                        # waiting 30 h: over the 24 h SLA
    seed_case("F1", 3, status="FAILED")
    seed_case("O1", 12, "Accept", "COMPLETED", final="Reject", reviewed_after_h=1)        # reviewer overturned an Accept
    d = dash(client, 7)
    k = d["kpis"]
    assert (k["total"], k["analyzed"], k["failed"]) == (10, 9, 1)
    assert (k["accept"], k["reject"], k["needs_review"]) == (4, 1, 4)
    assert k["auto_decided"] == 5 and k["straight_through_rate"] == round(5 / 9, 4)
    assert (k["overturned"], k["overturn_rate"]) == (1, 0.2)
    assert k["queue_reviewed"] == 2 and k["reviewed"] == 3
    assert k["median_review_hours"] == 4.0                                               # median of 2 h and 6 h, queue reviews only
    assert k["p50_ms"] == 20_000 and k["avg_confidence"] == 0.9
    f = d["funnel"]
    assert (f["received"], f["analyzed"], f["auto_decided"], f["sent_to_review"], f["reviewed"], f["waiting"], f["overturned"]) == (10, 9, 5, 4, 2, 2, 1)
    b = d["backlog"]
    assert b["pending"] == 2 and b["over_sla"] == 1 and b["oldest"][0]["filename"] == "N4.mp4"   # oldest first
    assert d["sla_hours"] == 24


def test_sla_is_a_setting(client):
    seed_case("N", 10, "Needs Review", "PENDING")
    assert dash(client)["backlog"]["over_sla"] == 0
    assert client.put("/api/config", json={"review_sla_hours": 4}).json()["review_sla_hours"] == 4
    d = dash(client)
    assert d["sla_hours"] == 4 and d["backlog"]["over_sla"] == 1


def test_previous_period_comparison(client):
    # window = last 7 days; previous = the 7 days before that
    for i in range(4):
        seed_case(f"C{i}", 24 * 2)                                                       # current: 4
    seed_case("C4", 24 * 3, "Needs Review", "PENDING", params=[("face_match", "Mismatch", 0.1)])
    for i in range(2):
        seed_case(f"P{i}", 24 * 9)                                                       # previous: 2
    seed_case("P2", 24 * 10, "Needs Review", "PENDING", params=[("face_match", "Match", 0.8)])
    seed_case("P3", 24 * 11, "Needs Review", "PENDING", params=[("face_match", "Match", 0.8)])
    seed_case("OLD", 24 * 40)                                                            # outside both windows
    d = dash(client, 7)
    assert d["kpis"]["analyzed"] == 5 and d["previous"]["kpis"]["analyzed"] == 4
    assert d["kpis"]["needs_review"] == 1 and d["previous"]["kpis"]["needs_review"] == 2
    fm = next(t for t in d["tests"] if t["key"] == "face_match")
    pf = d["previous"]["tests"]["face_match"]
    assert fm["attention_rate"] == 1.0 and pf["attention_rate"] == 0.0 and pf["total"] == 2
    assert dash(client, 0)["previous"] is None                                           # all time has nothing to compare with
    assert dash(client, 0)["kpis"]["analyzed"] == 10


def test_every_test_reads_the_same_way(client):
    seed_case("X1", 2, params=[("video_authenticity", "Pass", 0.02), ("age_estimation", "Estimated age", 30), ("liveness", "Low confidence", 0.6)])
    seed_case("X2", 3, params=[("video_authenticity", "Suspected Manipulation", 0.9), ("age_estimation", "Review", 60), ("liveness", "Unavailable", None)])
    seed_case("X3", 4, params=[("video_authenticity", "Review", 0.2), ("age_estimation", "Low confidence", 40), ("liveness", "Pass", 0.95)])
    d = dash(client)
    by = {t["key"]: t for t in d["tests"]}
    for t in d["tests"]:
        assert t["clean"] + t["attention"] + t["unassessed"] == t["total"], t["key"]
        if t["total"]:
            assert abs(t["clean_rate"] + t["attention_rate"] + t["unassessed_rate"] - 1) < 1e-3
    assert (by["video_authenticity"]["clean"], by["video_authenticity"]["attention"], by["video_authenticity"]["unassessed"]) == (1, 2, 0)
    assert (by["age_estimation"]["clean"], by["age_estimation"]["attention"], by["age_estimation"]["unassessed"]) == (1, 1, 1)
    assert (by["liveness"]["clean"], by["liveness"]["attention"], by["liveness"]["unassessed"]) == (1, 0, 2)
    assert by["health_indicators"]["total"] == 0 and by["health_indicators"]["attention_rate"] is None


def test_trend_has_a_continuous_axis(client):
    seed_case("D1", 24 * 2)
    seed_case("D2", 24 * 2, "Needs Review", "PENDING")
    seed_case("D3", 24 * 5, "Reject")
    d = dash(client, 7)
    assert d["bucket"] == "day" and len(d["trend"]) in (8, 9)                           # one entry per day, gaps filled with zeros
    days = [p["t"] for p in d["trend"]]
    assert days == sorted(days) and len(set(days)) == len(days)
    assert sum(p["Accept"] + p["Needs Review"] + p["Reject"] for p in d["trend"]) == 3
    assert sum(1 for p in d["trend"] if p["Accept"] + p["Needs Review"] + p["Reject"] == 0) >= len(days) - 2
    h = dash(client, 1)
    seed_case("H1", 1)
    h = dash(client, 1)
    assert h["bucket"] == "hour" and len(h["trend"]) == 24 and sum(p["Accept"] for p in h["trend"]) == 1
    assert dash(client, 0)["bucket"] == "day"


def test_top_exceptions_and_engines(client):
    for i in range(3):
        seed_case(f"E{i}", 2, "Needs Review", "PENDING", exceptions=[("face_match", "Face does not match the ID document photo")])
    seed_case("E9", 2, "Needs Review", "PENDING", exceptions=[("liveness", "Liveness unclear")])
    d = dash(client)
    assert d["top_exceptions"][0] == {"parameter": "face_match", "reason": "Face does not match the ID document photo", "count": 3}
    assert {e["key"] for e in d["engines"]} == {"video_authenticity", "health_indicators", "age_estimation", "face_match", "liveness"}
    assert all("ready" in e and "reason" in e for e in d["engines"])


def test_empty_dashboard_is_well_formed(client):
    d = dash(client, 30)
    k = d["kpis"]
    assert k["total"] == 0 and k["straight_through_rate"] is None and k["median_review_hours"] is None and k["p50_ms"] is None
    assert d["backlog"] == {"pending": 0, "over_sla": 0, "oldest": []} and d["top_exceptions"] == []
    assert len(d["tests"]) == 5 and all(t["total"] == 0 and t["attention_rate"] is None for t in d["tests"])
    assert sum(sum(p[x] for x in ("Accept", "Needs Review", "Reject")) for p in d["trend"]) == 0


# ---- a case being processed cannot be deleted or re-run (no race with the blob upload) ----------
def test_busy_cases_cannot_be_deleted_or_rerun(client, monkeypatch):
    from app import jobs
    (a,) = upload_ids(client, ("a.mp4", b"x"))
    wait_done(client, 1)
    monkeypatch.setattr(jobs, "is_busy", lambda cid: True)           # as if its post-analysis upload were still running
    monkeypatch.setattr(jobs, "cancel", lambda cid: False)           # ...and the worker could not be stopped
    r = bulk(client, "delete", [a]).json()
    assert r["failed"] == 1 and "right now" in r["results"][0]["message"]
    assert bulk(client, "rerun", [a]).json()["failed"] == 1
    assert client.post(f"/api/cases/{a}/analyze").status_code == 409
    assert client.get(f"/api/cases/{a}").status_code == 200
    monkeypatch.undo()


def test_a_queued_case_can_be_cancelled_and_deleted(client, monkeypatch):
    from app import jobs
    (a,) = upload_ids(client, ("a.mp4", b"x"))
    wait_done(client, 1)
    monkeypatch.setattr(jobs, "is_busy", lambda cid: True)           # still queued, not yet running...
    monkeypatch.setattr(jobs, "cancel", lambda cid: True)            # ...so it can be taken off the queue
    assert bulk(client, "delete", [a]).json()["succeeded"] == 1


def test_job_queue_busy_tracking():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app import jobs
    jobs.stop()
    jobs._pool = ThreadPoolExecutor(max_workers=1)
    gate, started = threading.Event(), threading.Event()
    original = jobs.analyze_case
    jobs.analyze_case = lambda db, cid, only=None, run_id=None: (started.set(), gate.wait(5))
    try:
        jobs.enqueue(1)                                              # starts running and blocks
        assert started.wait(5) and jobs.is_busy(1) and jobs.cancel(1) is False   # running: cannot be cancelled
        jobs.enqueue(2)                                              # waits behind it in the queue
        assert jobs.is_busy(2) and jobs.cancel(2) is True and not jobs.is_busy(2)   # queued: cancelled and idle
        assert jobs.cancel(99) is True                               # never queued: nothing to wait for
        gate.set()
        assert jobs.wait_idle(5) and not jobs.is_busy(1)
    finally:
        jobs.analyze_case = original
        gate.set()
        jobs._pool.shutdown(wait=True)
        jobs._pool = None
