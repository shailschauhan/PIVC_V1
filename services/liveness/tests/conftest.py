import os

# Must be set before cv2 is imported anywhere: OpenCV refuses images with more pixels than this (decompression-bomb guard).
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", "40000000")

from pathlib import Path  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KEY = "test-key-1"


def make_settings(**kw) -> Settings:
    return Settings(api_keys=f"{KEY},test-key-2", models_dir=ROOT / "models", **kw)


@pytest.fixture(scope="session")
def models_present():
    if not (ROOT / "models" / "face_detection_yunet_2023mar.onnx").exists():
        pytest.skip("YuNet not downloaded (run scripts/download_models.py)")


@pytest.fixture
def client(models_present):
    with TestClient(create_app(make_settings())) as c:
        c.headers["X-API-Key"] = KEY
        yield c


def png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def noise(w=400, h=400, seed=0) -> bytes:
    return png(np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8))


@pytest.fixture(scope="session")
def faces():
    """Optional real face images: set LIVENESS_TEST_FACES to a folder containing celeb_real_0.jpg and dfdc_real_0.jpg."""
    d = os.environ.get("LIVENESS_TEST_FACES")
    if not d or not Path(d).is_dir():
        pytest.skip("set LIVENESS_TEST_FACES to a folder of sample face images (not bundled for privacy/licence reasons)")
    return Path(d)
