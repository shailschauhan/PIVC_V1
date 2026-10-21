import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.decision import aggregate_frames, decide_pair
from app.main import create_app

from .conftest import KEY, make_settings, noise, png

M, MM = 0.45, 0.28


# ---------- decision logic (no models needed) ----------
def test_pair_decision_bands():
    assert decide_pair(0.80, M, MM, [], [])["decision"] == "match"
    assert decide_pair(0.45, M, MM, [], [])["decision"] == "match"          # boundary is inclusive
    assert decide_pair(0.30, M, MM, [], [])["decision"] == "review"
    assert decide_pair(0.10, M, MM, [], [])["decision"] == "mismatch"
    assert decide_pair(None, M, MM, [], [])["decision"] == "no_face"


def test_poor_quality_can_never_prove_a_mismatch_but_does_not_block_a_match():
    for flags in (["too_small"], ["blurry"], ["off_angle"]):
        assert decide_pair(0.05, M, MM, flags, [])["decision"] == "review"
        assert decide_pair(0.05, M, MM, [], flags)["decision"] == "review"
    assert decide_pair(0.05, M, MM, ["low_detection_score"], [])["decision"] == "mismatch"   # not a severe flag
    assert decide_pair(0.90, M, MM, ["too_small"], ["blurry"])["decision"] == "match"


def frame(sim, flags=(), face=True):
    return {"face": face, "similarity": sim, "quality_flags": list(flags)}


def agg(rows, ref_flags=(), min_usable=3):
    return aggregate_frames(rows, M, MM, list(ref_flags), min_usable)


def test_video_aggregation_match_mismatch_review():
    assert agg([frame(0.8)] * 6)["decision"] == "match"
    assert agg([frame(0.05)] * 6)["decision"] == "mismatch"
    assert agg([frame(0.35)] * 6)["decision"] == "review"
    mixed = agg([frame(0.8)] * 3 + [frame(0.05)] * 3)               # median in the middle, half the frames match
    assert mixed["decision"] in ("match", "review") and mixed["fraction_frames_matching"] == 0.5


def test_video_aggregation_ignores_bad_frames_when_enough_good_ones_exist():
    rows = [frame(0.8)] * 4 + [frame(0.02, ["blurry"])] * 6
    r = agg(rows)
    assert r["decision"] == "match" and r["frames_usable"] == 4 and r["low_quality"] is False


def test_video_aggregation_low_quality_never_mismatches():
    r = agg([frame(0.02, ["too_small"])] * 8)                       # no usable frame at all
    assert r["decision"] == "review" and r["low_quality"] is True
    assert agg([frame(0.02)] * 8, ref_flags=["blurry"])["decision"] == "review"   # blurry reference photo
    assert agg([frame(None, face=False)] * 5)["decision"] == "no_face"
    assert agg([frame(0.8, face=True)] * 2, min_usable=3)["low_quality"] is True


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
    for method, path in [("get", "/v1/info"), ("post", "/v1/faces/detect"), ("post", "/v1/faces/compare"), ("post", "/v1/faces/compare-video")]:
        assert getattr(anon, method)(path).status_code == 401, path
        assert getattr(anon, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert anon.get("/v1/info", headers={"X-API-Key": "test-key-2"}).status_code == 200    # second (rotation) key works
    assert client.get("/health").headers["X-Request-ID"]


def test_info_reports_models_and_thresholds(client):
    j = client.get("/v1/info").json()
    assert set(j["models"]) == {"face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"}
    assert all(len(m["sha256"]) == 64 for m in j["models"].values())
    assert j["thresholds"] == {"match": M, "mismatch": MM} and j["limitations"]


# ---------- input safety ----------
def up(name, data, ctype="image/png"):
    return (name, data, ctype)


def test_detect_finds_nothing_in_noise(client):
    r = client.post("/v1/faces/detect", files={"image": up("n.png", noise())})
    assert r.status_code == 200 and r.json()["faces_found"] == 0 and r.json()["image_size"] == [400, 400]


def test_compare_without_faces_is_no_face_not_an_error(client):
    r = client.post("/v1/faces/compare", files={"image_a": up("a.png", noise(seed=1)), "image_b": up("b.png", noise(seed=2))})
    j = r.json()
    assert r.status_code == 200 and j["decision"] == "no_face" and j["similarity"] is None and j["faces_found"] == {"a": 0, "b": 0}


def test_rejects_bad_uploads(client):
    assert client.post("/v1/faces/detect", files={"image": up("x.txt", b"hello", "text/plain")}).status_code == 415
    assert client.post("/v1/faces/detect", files={"image": up("x.png", b"")}).status_code == 422
    assert client.post("/v1/faces/detect", files={"image": up("x.png", b"definitely not an image")}).status_code == 422
    assert client.post("/v1/faces/detect").status_code == 422                                  # missing file
    huge = png(np.zeros((9000, 9000, 3), np.uint8))                                            # 81 MP: compresses small, decodes huge
    assert len(huge) < 10 * 1024 * 1024
    assert client.post("/v1/faces/detect", files={"image": up("huge.png", huge)}).status_code == 422


def test_size_limits(models_present):
    with TestClient(create_app(make_settings(max_image_mb=1, max_video_mb=1))) as c:
        c.headers["X-API-Key"] = KEY
        big = png(np.random.default_rng(0).integers(0, 255, (1200, 1200, 3), dtype=np.uint8))
        assert len(big) > 1024 * 1024
        assert c.post("/v1/faces/detect", files={"image": up("big.png", big)}).status_code == 413
        v = ("v.mp4", b"x" * (2 * 1024 * 1024), "video/mp4")
        assert c.post("/v1/faces/compare-video", files={"video": v, "reference": up("r.png", noise())}).status_code == 413


def test_threshold_and_frame_validation(client):
    files = {"image_a": up("a.png", noise()), "image_b": up("b.png", noise())}
    for bad in ({"match_threshold": "0.3", "mismatch_threshold": "0.5"}, {"match_threshold": "1.5"}, {"mismatch_threshold": "-0.1"}):
        assert client.post("/v1/faces/compare", files=files, data=bad).status_code == 422, bad
    assert client.post("/v1/faces/compare", files=files, data={"match_threshold": "0.6", "mismatch_threshold": "0.2"}).json()["thresholds"] == {"match": 0.6, "mismatch": 0.2}
    v = ("v.mp4", b"not a video", "video/mp4")
    assert client.post("/v1/faces/compare-video", files={"video": v, "reference": up("r.png", noise())}, data={"frames": "0"}).status_code == 422
    assert client.post("/v1/faces/compare-video", files={"video": v, "reference": up("r.png", noise())}, data={"frames": "999"}).status_code == 422
    assert client.post("/v1/faces/compare-video", files={"video": v, "reference": up("r.png", noise())}).status_code == 422   # undecodable video
    assert client.post("/v1/faces/compare-video", files={"video": ("v.txt", b"x", "text/plain"), "reference": up("r.png", noise())}).status_code == 415


# ---------- real faces (needs FACE_MATCH_TEST_FACES) ----------
def jpg(faces, name):
    return (name, (faces / name).read_bytes(), "image/jpeg")


def test_same_person_matches_and_different_people_do_not(client, faces):
    same = client.post("/v1/faces/compare", files={"image_a": jpg(faces, "celeb_real_0.jpg"), "image_b": jpg(faces, "celeb_real_2.jpg")}).json()
    assert same["decision"] == "match" and same["similarity"] > 0.6 and same["faces_found"] == {"a": 1, "b": 1}
    diff = client.post("/v1/faces/compare", files={"image_a": jpg(faces, "celeb_real_0.jpg"), "image_b": jpg(faces, "ffpp_real_0.jpg")}).json()
    assert diff["decision"] == "mismatch" and diff["similarity"] < 0.28
    assert client.post("/v1/faces/compare", files={"image_a": jpg(faces, "celeb_real_0.jpg"), "image_b": jpg(faces, "celeb_real_0.jpg")}).json()["similarity"] > 0.99


def test_tiny_blurry_faces_cannot_prove_a_mismatch(client, faces):
    r = client.post("/v1/faces/compare", files={"image_a": jpg(faces, "dfdc_real_2.jpg"), "image_b": jpg(faces, "celeb_real_0.jpg")}).json()
    assert "too_small" in r["quality"]["a"]["flags"] and r["decision"] in ("review", "no_face")


def test_detect_reports_box_landmarks_and_quality(client, faces):
    j = client.post("/v1/faces/detect", files={"image": jpg(faces, "celeb_real_0.jpg")}).json()
    f = j["faces"][0]
    assert j["faces_found"] == 1 and len(f["box"]) == 4 and f["score"] > 0.8
    assert set(f["landmarks"]) == {"right_eye", "left_eye", "nose", "right_mouth", "left_mouth"}
    assert set(f["quality"]) >= {"face_size_px", "sharpness", "yaw", "roll_deg", "flags"}
    assert 0 < f["box"][0] < j["image_size"][0] and f["quality"]["face_size_px"] > 100      # reported in the original image's pixels


def make_video(path, imgs, per=8):
    h, w = imgs[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h))
    for im in imgs:
        for _ in range(per):
            vw.write(cv2.resize(im, (w, h)))
    vw.release()


def test_video_against_reference(client, faces, tmp_path):
    frames = [cv2.imread(str(faces / f"celeb_real_{i}.jpg")) for i in range(3)]
    path = tmp_path / "v.mp4"
    make_video(path, frames)
    data = path.read_bytes()
    ok = client.post("/v1/faces/compare-video", files={"video": ("v.mp4", data, "video/mp4"), "reference": jpg(faces, "celeb_real_0.jpg")}, data={"frames": "9"}).json()
    assert ok["decision"] == "match" and ok["similarity"] > 0.6 and ok["frames_scored"] >= 6 and len(ok["frames"]) <= 9
    other = client.post("/v1/faces/compare-video", files={"video": ("v.mp4", data, "video/mp4"), "reference": jpg(faces, "ffpp_real_0.jpg")}).json()
    assert other["decision"] == "mismatch" and other["similarity"] < 0.28
    assert other["reference"]["faces_found"] == 1
    none = client.post("/v1/faces/compare-video", files={"video": ("v.mp4", data, "video/mp4"), "reference": ("r.png", noise(), "image/png")}).json()
    assert none["decision"] == "no_face"
