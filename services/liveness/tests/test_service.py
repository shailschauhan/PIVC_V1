import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.decision import aggregate
from app.engine import new_box
from app.main import create_app

from .conftest import KEY, make_settings, noise, png

P, SP = 0.80, 0.30


# ---------- decision logic (no models needed) ----------
def fr(p, flags=(), face=True):
    return {"face": face, "real_probability": p, "flags": list(flags)}


def agg(rows, min_usable=3):
    return aggregate(rows, P, SP, min_usable)


def test_pass_needs_a_high_median_and_most_frames_real():
    r = agg([fr(0.97)] * 8)
    assert r["decision"] == "pass" and r["spoof_suspected"] is False and r["real_probability"] == 0.97
    assert agg([fr(0.95)] * 4 + [fr(0.2)] * 4)["decision"] == "review"       # half the frames look fake
    assert agg([fr(0.6)] * 8)["decision"] == "review"                          # mediocre scores are not a pass


def test_a_single_odd_frame_does_not_flip_the_result():
    assert agg([fr(0.99)] * 9 + [fr(0.05)])["decision"] == "pass"              # the median ignores one false reject
    assert agg([fr(0.99)] * 9 + [fr(0.05)])["real_probability_min"] == 0.05


def test_spoof_suspected_only_when_evidence_is_consistent_and_never_a_hard_fail():
    r = agg([fr(0.05)] * 8)
    assert r["decision"] == "review" and r["spoof_suspected"] is True and "presentation attack" in r["reason"]
    assert agg([fr(0.05)] * 4 + [fr(0.9)] * 4)["spoof_suspected"] is False    # mixed -> just review


def test_unjudgeable_frames_are_ignored_and_too_few_good_frames_is_low_quality():
    rows = [fr(0.99)] * 4 + [fr(0.02, ["tight_framing"])] * 6
    r = agg(rows)
    assert r["decision"] == "pass" and r["frames_usable"] == 4 and r["low_quality"] is False
    low = agg([fr(0.99, ["too_small"])] * 8)                                   # nothing judgeable: even 0.99 is not trusted
    assert low["decision"] == "review" and low["low_quality"] is True and low["spoof_suspected"] is False
    assert agg([fr(0.01, ["too_small"])] * 8)["spoof_suspected"] is False      # ...and poor frames cannot raise a spoof alarm either


def test_no_face():
    r = agg([fr(None, face=False)] * 5)
    assert r["decision"] == "no_face" and r["real_probability"] is None and r["frames_with_face"] == 0


def test_crop_box_stays_inside_the_frame_and_shrinks_near_borders():
    for (w, h, box, scale) in [(640, 480, (300, 200, 100, 120), 2.7), (640, 480, (0, 0, 100, 100), 4.0), (640, 480, (560, 380, 80, 99), 2.7),
                               (200, 200, (20, 20, 180, 180), 4.0)]:
        lx, ly, rx, ry, eff = new_box(w, h, box, scale)
        assert 0 <= lx < rx <= w - 1 and 0 <= ly < ry <= h - 1 and eff <= scale
    assert new_box(640, 480, (300, 200, 100, 120), 2.7)[4] == 2.7                # plenty of room: full scale
    assert new_box(200, 200, (20, 20, 180, 180), 4.0)[4] < 1.2                   # face fills the frame: scale collapses


# ---------- startup and auth ----------
def test_refuses_to_start_without_keys_unless_auth_is_disabled(models_present):
    with pytest.raises(RuntimeError, match="API key"):
        with TestClient(create_app(Settings(api_keys="", disable_auth=False))):
            pass
    with TestClient(create_app(Settings(api_keys="", disable_auth=True))) as c:
        assert c.get("/v1/info").status_code == 200


def test_missing_models_give_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="download_models"):
        with TestClient(create_app(Settings(api_keys="k", models_dir=tmp_path))):
            pass


def test_api_key_required_on_v1_but_not_health(client):
    anon = TestClient(client.app)
    assert anon.get("/health").json()["status"] == "ok"
    for method, path in [("get", "/v1/info"), ("post", "/v1/liveness/image"), ("post", "/v1/liveness/video")]:
        assert getattr(anon, method)(path).status_code == 401, path
        assert getattr(anon, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert anon.get("/v1/info", headers={"X-API-Key": "test-key-2"}).status_code == 200
    assert client.get("/health").headers["X-Request-ID"]


def test_info_lists_both_models_and_the_honest_limitations(client):
    j = client.get("/v1/info").json()
    assert sorted(m["scale"] for m in j["models"].values()) == [2.7, 4.0]
    assert j["thresholds"] == {"pass": P, "spoof": SP}
    assert any("not proof" in x for x in j["limitations"]) and any("active" in x for x in j["limitations"])


# ---------- input safety ----------
def up(name, data, ctype="image/png"):
    return (name, data, ctype)


def test_noise_has_no_face(client):
    r = client.post("/v1/liveness/image", files={"image": up("n.png", noise())})
    j = r.json()
    assert r.status_code == 200 and j["decision"] == "no_face" and j["faces_found"] == 0 and j["face"] is None and j["spoof_suspected"] is False


def test_rejects_bad_uploads(client):
    assert client.post("/v1/liveness/image", files={"image": up("x.txt", b"hello", "text/plain")}).status_code == 415
    assert client.post("/v1/liveness/image", files={"image": up("x.png", b"")}).status_code == 422
    assert client.post("/v1/liveness/image", files={"image": up("x.png", b"not an image at all")}).status_code == 422
    assert client.post("/v1/liveness/image").status_code == 422
    assert client.post("/v1/liveness/image", files={"image": up("huge.png", png(np.zeros((9000, 9000, 3), np.uint8)))}).status_code == 422
    assert client.post("/v1/liveness/video", files={"video": ("v.txt", b"x", "text/plain")}).status_code == 415
    assert client.post("/v1/liveness/video", files={"video": ("v.mp4", b"not a video", "video/mp4")}).status_code == 422


def test_size_limits(models_present):
    with TestClient(create_app(make_settings(max_image_mb=1, max_video_mb=1))) as c:
        c.headers["X-API-Key"] = KEY
        big = png(np.random.default_rng(0).integers(0, 255, (1200, 1200, 3), dtype=np.uint8))
        assert c.post("/v1/liveness/image", files={"image": up("big.png", big)}).status_code == 413
        assert c.post("/v1/liveness/video", files={"video": ("v.mp4", b"x" * (2 * 1024 * 1024), "video/mp4")}).status_code == 413


def test_threshold_and_frame_validation(client):
    f = {"image": up("a.png", noise())}
    for bad in ({"pass_threshold": "0.3", "spoof_threshold": "0.5"}, {"pass_threshold": "1.5"}, {"spoof_threshold": "-0.1"}):
        assert client.post("/v1/liveness/image", files=f, data=bad).status_code == 422, bad
    v = {"video": ("v.mp4", b"x", "video/mp4")}
    assert client.post("/v1/liveness/video", files=v, data={"frames": "0"}).status_code == 422
    assert client.post("/v1/liveness/video", files=v, data={"frames": "999"}).status_code == 422


# ---------- real faces (needs LIVENESS_TEST_FACES) ----------
def jpg(faces, name):
    return (name, (faces / name).read_bytes(), "image/jpeg")


def test_genuine_face_scores_real_and_reports_per_model_detail(client, faces):
    j = client.post("/v1/liveness/image", files={"image": jpg(faces, "celeb_real_0.jpg")}).json()
    f = j["face"]
    assert j["faces_found"] == 1 and f["label"] == "real" and f["real_probability"] > 0.9
    assert len(f["models"]) == 2 and all(len(m["probs"]) == 3 and abs(sum(m["probs"]) - 1) < 1e-3 for m in f["models"])
    assert "tight_framing" in f["flags"] and j["decision"] == "review"       # close-up: too little context to trust a pass on one frame


def test_tiny_faces_are_flagged_and_not_trusted(client, faces):
    f = client.post("/v1/liveness/image", files={"image": jpg(faces, "dfdc_real_0.jpg")}).json()["face"]
    assert "too_small" in f["flags"]


def make_video(path, imgs, per=8):
    h, w = imgs[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h))
    for im in imgs:
        for _ in range(per):
            vw.write(cv2.resize(im, (w, h)))
    vw.release()


def test_video_summary_for_a_close_up_clip(client, faces, tmp_path):
    path = tmp_path / "v.mp4"
    make_video(path, [cv2.imread(str(faces / f"celeb_real_{i}.jpg")) for i in range(3)])
    j = client.post("/v1/liveness/video", files={"video": ("v.mp4", path.read_bytes(), "video/mp4")}, data={"frames": "9"}).json()
    assert j["frames_with_face"] >= 6 and len(j["frames"]) <= 9 and all(0 <= (r["real_probability"] or 0) <= 1 for r in j["frames"])
    assert j["low_quality"] is True and j["decision"] == "review" and j["spoof_suspected"] is False   # every frame is a tight close-up
    assert j["real_probability"] > 0.9 and "Too few good frames" in j["reason"]
