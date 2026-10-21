"""Download the age-estimation models into backend/models/age/ and record their SHA-256.

  mivolo_v2/              iitolstykh/mivolo_v2                  (Apache-2.0, safetensors + small custom python files)
  yolov8x_person_face.pt  iitolstykh/YOLO-Face-Person-Detector  (AGPL-3.0, ultralytics checkpoint = a pickle file)

Both are pinned to a revision so what you download is what was reviewed. Run it with the age environment:
  backend/.venv-age/Scripts/python backend/scripts/download_age_models.py
"""
import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

DEST = Path(__file__).resolve().parents[1] / "models" / "age"
MIVOLO = ("iitolstykh/mivolo_v2", "REPLACED_AT_PIN")
DETECTOR = ("iitolstykh/YOLO-Face-Person-Detector", "REPLACED_AT_PIN")

# Revisions the integration was tested with (see README, "Age estimation").
PINS = json.loads((Path(__file__).with_name("age_models_pins.json")).read_text()) if Path(__file__).with_name("age_models_pins.json").exists() else {}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    rev_m = PINS.get("mivolo_v2") or None
    rev_d = PINS.get("detector") or None
    snapshot_download(MIVOLO[0], revision=rev_m, local_dir=DEST / "mivolo_v2",
                      allow_patterns=["*.json", "*.py", "*.safetensors", "*.txt", "README.md"])
    det = Path(hf_hub_download(DETECTOR[0], "yolov8x_person_face.pt", revision=rev_d, local_dir=DEST))
    manifest = {"mivolo_v2_revision": rev_m, "detector_revision": rev_d, "files": {}}
    for p in sorted((DEST / "mivolo_v2").rglob("*")) + [det]:
        if p.is_file() and ".cache" not in p.parts:
            manifest["files"][str(p.relative_to(DEST)).replace("\\", "/")] = sha256(p)
    (DEST / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
