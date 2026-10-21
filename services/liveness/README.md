# Liveness service

Independent FastAPI service: **does this image / video show a real, present face rather than a printed photo, a screen or a mask?**
Passive, single-frame anti-spoofing with **MiniFASNet** (Minivision, [Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing),
Apache-2.0) and the YuNet face detector. CPU only (`onnxruntime`); no data is stored.

> **Read this first.** For recorded footage this is a **risk signal, not proof**. It cannot match a capture-time, challenge-response liveness check
> (e.g. Azure Face liveness or a similar SDK), which needs a live session. Use it to route doubtful videos to a human, never to approve or
> reject on its own. See *What was measured*.

## Run

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
python scripts/download_models.py          # only YuNet (0.2 MB); the two MiniFASNet ONNX files are in models/
export LIVENESS_API_KEYS=some-long-random-key
uvicorn app.main:app --port 8102           # docs at http://localhost:8102/docs
```
Refuses to start without an API key (`LIVENESS_DISABLE_AUTH=true` for local development only). One worker per process; scale with replicas.

## API (all `/v1` routes need the `X-API-Key` header; `/health` is open)

| Endpoint | Input (multipart) | Result |
|---|---|---|
| `GET /health` | | `{"status": "ok"}` |
| `GET /v1/info` | | models, thresholds, limits, licences, **limitations** |
| `POST /v1/liveness/image` | `image` [, `pass_threshold`, `spoof_threshold`] | `decision`, `real_probability`, `spoof_suspected`, face box, per-model probabilities, quality flags |
| `POST /v1/liveness/video` | `video` [, `frames`, thresholds] | per-frame scores + one aggregated `decision` |

**Decisions:** `pass` | `review` | `no_face`. The service never returns a hard "fail": evidence of a presentation attack is reported as
`spoof_suspected: true` (consistently low scores on judgeable frames) together with `decision: "review"`. A `pass` needs a high median real-face
probability *and* most frames real. Frames the models cannot judge are ignored: `too_small` (face under 60 px) and `tight_framing` (not enough
surrounding context for the model's 2.7x / 4.0x crop, typical of close-ups and ID-style photos). If fewer than `min_usable_frames` remain the
result is `review` with `low_quality: true` (this is common for webcam close-ups).

```bash
curl -H "X-API-Key: $KEY" -F video=@pivc.mp4 -F frames=12 http://localhost:8102/v1/liveness/video
```

## How it works (faithful to upstream)
For each model: crop the face box expanded by the model's scale (2.7 / 4.0), resize to 80x80, feed raw BGR 0-255 values (no mean/std), softmax over
3 classes (class 1 = real), sum the two models. Upstream finds the face with a RetinaFace caffemodel; we use YuNet, whose boxes are similar but not identical.
The `.pth` checkpoints are converted to ONNX by `tools/export_onnx.py` (checksums in `models/MANIFEST.json`).
**Verified:** the crop code equals upstream's `CropImage` on 600 random frame/box cases; ONNX output equals upstream PyTorch within 2e-6 (random inputs)
and 7e-5 (real faces).

## Configuration (`LIVENESS_*`, see `.env.example`)
`API_KEYS`, `DISABLE_AUTH`, `MODELS_DIR`, `MAX_IMAGE_MB`, `MAX_VIDEO_MB`, `MAX_PIXELS`, `DEFAULT_FRAMES` (12), `MAX_FRAMES` (60),
`DET_SCORE_THRESHOLD` (0.7), `MIN_FACE_PX` (60), `PASS_THRESHOLD` (0.80), `SPOOF_THRESHOLD` (0.30), `MIN_USABLE_FRAMES` (3).

## What was measured, and what was not
- On 18 still frames from six sample clips (genuine recordings of real people, plus face-swapped deepfakes; **no spoof attacks**): most frames scored
  0.72-1.0 "real", but one genuine frame scored 0.48 (labelled spoof) and several 0.8-0.85, so single-frame false rejects happen. The median over frames
  absorbs isolated ones.
- **Face-swapped deepfakes score about 1.0 "real"**: this is not a deepfake detector (use the main app's video-authenticity test for that).
- A crude synthetic "low-resolution print" made from a real frame still scored 0.92 "real". That is not a real attack, but together with the missing
  attack data it means **spoof detection is unmeasured**: do not assume it catches printed photos, screens or masks. Upstream also notes weaker
  results on 3D masks.
- The defaults (0.80 / 0.30) are uncalibrated. Collect genuine and attack recordings from your own capture pipeline, then choose thresholds from the error rates.
- Accuracy on compressed video-call footage, other cameras and lighting is unknown; passive models are known to generalise poorly across devices.

## Safety, licences
Same input safety as the Face Match service (size and type limits, decompression-bomb guard, temp files deleted, no content logging, constant-time keys, non-root container).
Weights: MiniFASNet from Minivision under Apache-2.0 (`LICENSE-Silent-Face-Anti-Spoofing`); YuNet MIT (OpenCV Zoo). Confirm against the upstream repositories
before commercial use. Processing customers' faces needs consent and DPDP / IRDAI review.

## Tests
```bash
pip install -r requirements.txt && python scripts/download_models.py && pytest -q            # 14 run, 3 skip
LIVENESS_TEST_FACES=/path/to/faces pytest -q       # +3 real-face tests (celeb_real_0.jpg ... dfdc_real_0.jpg)
```
