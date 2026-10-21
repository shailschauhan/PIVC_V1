"""Download the YuNet face detector (OpenCV Zoo) into ../models and verify its SHA-256.

The two anti-spoofing models (MiniFASNetV2 scale 2.7 and MiniFASNetV1SE scale 4.0) are already in ../models as ONNX files,
converted from the unmodified upstream checkpoints with tools/export_onnx.py (see models/MANIFEST.json for checksums).
Usage:  python scripts/download_models.py   (add --force to re-download)
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

NAME = "face_detection_yunet_2023mar.onnx"
URL = "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"
SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"  # recorded on first download (trust-on-first-use)
DEST = Path(__file__).resolve().parents[1] / "models" / NAME


def main(force: bool = False) -> int:
    DEST.parent.mkdir(exist_ok=True)
    if force or not DEST.exists():
        print(f"downloading {NAME} ...")
        tmp = DEST.with_suffix(".part")
        urllib.request.urlretrieve(URL, tmp)  # noqa: S310 - fixed https URL above
        tmp.replace(DEST)
    got = hashlib.sha256(DEST.read_bytes()).hexdigest()
    print(f"{'OK ' if got == SHA256 else 'MISMATCH'} {NAME}  sha256={got}")
    return 0 if got == SHA256 else 1


if __name__ == "__main__":
    sys.exit(main("--force" in sys.argv))
