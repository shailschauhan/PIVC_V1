# Face Match service

Independent FastAPI service: **is the person in this video / photo the same person as in that photo?**
(e.g. a PIVC video against the photograph on the customer's ID document). CPU only; no data is stored.

Models (OpenCV Zoo): **YuNet** face detector + **SFace** face recogniser, run through OpenCV's built-in DNN wrappers
(`FaceDetectorYN`, `FaceRecognizerSF`). Similarity is the cosine of two 128-d embeddings.

## Run

```bash
python -m venv .venv && .venv/Scripts/activate        # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python scripts/download_models.py                       # ~39 MB, SHA-256 verified, into models/
export FACE_MATCH_API_KEYS=some-long-random-key          # PowerShell: $env:FACE_MATCH_API_KEYS="..."
uvicorn app.main:app --port 8101                         # docs at http://localhost:8101/docs
```
The service **refuses to start without an API key** (set `FACE_MATCH_DISABLE_AUTH=true` for local development only).
One worker per process (the models are guarded by a lock); scale with replicas. `docker build -t face-match .` also works.

## API (all `/v1` routes need the `X-API-Key` header; `/health` is open)

| Endpoint | Input (multipart) | Result |
|---|---|---|
| `GET /health` | | `{"status": "ok"}` |
| `GET /v1/info` | | models + SHA-256, default thresholds, limits, licences, limitations |
| `POST /v1/faces/detect` | `image` | every face: box, 5 landmarks, score and **quality** (size, sharpness, yaw, roll, flags) |
| `POST /v1/faces/compare` | `image_a`, `image_b` [, `match_threshold`, `mismatch_threshold`] | `decision`, `similarity`, `reason`, quality of both |
| `POST /v1/faces/compare-video` | `video`, `reference` [, `frames`, thresholds] | per-frame similarities + one aggregated `decision` |

```bash
curl -H "X-API-Key: $KEY" -F image_a=@selfie.jpg -F image_b=@id_card.jpg http://localhost:8101/v1/faces/compare
# {"decision":"match","similarity":0.8777,"reason":"Similarity 0.88 is at or above the match threshold 0.45.", ...}

curl -H "X-API-Key: $KEY" -F video=@pivc.mp4 -F reference=@id_photo.jpg -F frames=12 http://localhost:8101/v1/faces/compare-video
```

**Decisions:** `match` | `mismatch` | `review` | `no_face`. Between the two thresholds the answer is `review`. A **poor-quality** image
(tiny, blurry or turned-away face) can lower the score, so it can never produce `mismatch`: that becomes `review`.
For video, only good frames are used when there are enough (`min_usable_frames`); the median similarity and the share of
matching frames decide. The main face in an image is the largest, most confident one (ID cards often contain several small faces).
Small images (typical for ID photos) are upscaled before detection.

## Configuration (`FACE_MATCH_*` environment variables, see `.env.example`)
`API_KEYS`, `DISABLE_AUTH`, `MODELS_DIR`, `MAX_IMAGE_MB` (10), `MAX_VIDEO_MB` (500), `MAX_PIXELS`, `DEFAULT_FRAMES` (12), `MAX_FRAMES` (60),
`DET_SCORE_THRESHOLD` (0.7), `MIN_FACE_PX` (60), `MIN_SHARPNESS` (25), `MAX_YAW` (0.45), `MATCH_THRESHOLD` (0.45),
`MISMATCH_THRESHOLD` (0.28), `MIN_USABLE_FRAMES` (3).

## Safety
Size limits (413), type allow-list (415), undecodable/empty files (422), a decompression-bomb guard (`OPENCV_IO_MAX_IMAGE_PIXELS`, 40 MP), uploads
kept in memory or a temp file deleted when the request ends, content never logged, constant-time key comparison, a non-root container.

## What was measured, and what was not
- On 18 frames from six sample clips (three different people, real and face-swapped): same-person frame pairs had a median similarity of 0.84,
  different people reached at most 0.23, and a real clip against its own face-swapped version at most 0.29. These are *easy* pairs (same video, same camera).
- **Not measured:** ID-photo-versus-selfie pairs, which are harder (small compressed photos, watermarks, age gap). The 0.45 / 0.28 defaults are
  cautious guesses around the easy-case numbers; **calibrate on your own labelled pairs before relying on decisions.**
- It is **not a liveness check**: a photograph of a person matches like the person does (see the Liveness service).

## Licences and provenance
OpenCV Zoo: YuNet (MIT) and SFace (Apache-2.0), as stated by OpenCV Zoo and its model pages; confirm against the upstream repository before
commercial use. Models are not stored in git; `scripts/download_models.py` fetches them and verifies pinned SHA-256 checksums (recorded on first download).
Face matching of customers is biometric processing: obtain consent and complete DPDP / IRDAI review.

## Tests
```bash
pip install -r requirements.txt && python scripts/download_models.py && pytest -q          # 14 tests run, 4 skip
FACE_MATCH_TEST_FACES=/path/to/faces pytest -q    # +4 real-face tests (celeb_real_0.jpg, celeb_real_2.jpg, ffpp_real_0.jpg, dfdc_real_2.jpg, ...)
```
Sample faces are not bundled (privacy / dataset licences).
