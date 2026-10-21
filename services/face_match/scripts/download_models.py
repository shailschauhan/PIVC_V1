"""Download the two OpenCV Zoo models used by the face-match service into ../models and verify their SHA-256.

  face_detection_yunet_2023mar.onnx      YuNet face detector        (OpenCV Zoo, MIT / Apache-2.0 - see README for licence notes)
  face_recognition_sface_2021dec.onnx    SFace face recogniser      (OpenCV Zoo, Apache-2.0)

Usage:  python scripts/download_models.py            (add --force to re-download)
The checksums below were recorded from the first download (trust-on-first-use, Oct 2026) and compared with the
upstream file sizes; they make later downloads refuse a changed or tampered file. Cross-check them against the
OpenCV Zoo repository if you need independent assurance.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}
DEST = Path(__file__).resolve().parents[1] / "models"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(force: bool = False) -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name, (url, want) in MODELS.items():
        path = DEST / name
        if force or not path.exists():
            print(f"downloading {name} ...")
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)  # noqa: S310 - fixed https URLs above
            tmp.replace(path)
        got = sha256(path)
        ok = got == want
        print(f"{'OK ' if ok else 'MISMATCH'} {name}  {path.stat().st_size / 1e6:.2f} MB  sha256={got}")
        if not ok:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main("--force" in sys.argv))
