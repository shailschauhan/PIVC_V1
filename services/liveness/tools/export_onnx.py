"""Convert the upstream Silent-Face-Anti-Spoofing (Minivision, Apache-2.0) PyTorch checkpoints to ONNX.

The service itself only needs onnxruntime; this one-off tool needs PyTorch and is how the committed .onnx files were made:

  python tools/export_onnx.py            # reads tools/upstream_pth/*.pth, writes models/*.onnx, checks ONNX == PyTorch

The .pth files are unmodified copies from https://github.com/minivision-ai/Silent-Face-Anti-Spoofing
(resources/anti_spoof_models/). Their SHA-256 and those of the ONNX outputs are written to models/MANIFEST.json.
"""
import hashlib
import json
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "minifasnet"))
import MiniFASNet as M  # noqa: E402  (vendored upstream file)

OUT = HERE.parent / "models"
ARCH = {"MiniFASNetV1": M.MiniFASNetV1, "MiniFASNetV2": M.MiniFASNetV2, "MiniFASNetV1SE": M.MiniFASNetV1SE, "MiniFASNetV2SE": M.MiniFASNetV2SE}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def parse(name: str) -> tuple[int, int, str, float | None]:
    """'2.7_80x80_MiniFASNetV2.pth' -> (80, 80, 'MiniFASNetV2', 2.7)   (same rule as upstream utility.parse_model_name)"""
    parts = name.split("_")[:-1]
    h, w = parts[-1].split("x")
    return int(h), int(w), name.split(".pth")[0].split("_")[-1], (None if parts[0] == "org" else float(parts[0]))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    manifest = {}
    for pth in sorted((HERE / "upstream_pth").glob("*.pth")):
        h, w, kind, scale = parse(pth.name)
        kernel = ((h + 15) // 16, (w + 15) // 16)
        model = ARCH[kind](conv6_kernel=kernel)
        state = torch.load(pth, map_location="cpu")
        state = OrderedDict((k[7:] if k.startswith("module.") else k, v) for k, v in state.items())
        model.load_state_dict(state)
        model.eval()
        out = OUT / (pth.stem + ".onnx")
        dummy = torch.zeros(1, 3, h, w)
        torch.onnx.export(model, dummy, str(out), input_names=["input"], output_names=["logits"], opset_version=17,
                          dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}}, dynamo=False)
        # the ONNX graph must reproduce the PyTorch outputs
        x = torch.rand(4, 3, h, w) * 255
        with torch.no_grad():
            ref = model(x).numpy()
        got = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"]).run(None, {"input": x.numpy()})[0]
        diff = float(np.abs(ref - got).max())
        assert diff < 1e-3, f"{pth.name}: ONNX differs from PyTorch by {diff}"
        manifest[out.name] = {"source_pth": pth.name, "source_pth_sha256": sha256(pth), "onnx_sha256": sha256(out),
                              "scale": scale, "input": [h, w], "arch": kind, "max_abs_diff_vs_pytorch": diff}
        print(f"{out.name}: ok (max |onnx - torch| = {diff:.2e}), scale={scale}, {out.stat().st_size / 1e6:.2f} MB")
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
