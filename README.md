# PIVC Video Authenticity

Upload PIVC videos (optionally with the customer's ID document photo); every video gets up to five tests and one report:
1. **Video authenticity** - AI-generated / manipulated content, using the DFSpot ensemble from
   [DeepFakeSol](https://github.com/shailschauhan/DeepFakeSol). This drives the Accept / Reject / Needs Review decision.
2. **Health indicators** - contactless vital signs (heart rate, breathing, HRV) from the face using
   [open-rppg](https://github.com/KegangWangCCNU/open-rppg). Reported with a signal-quality gate; informational by default.

3. **Age estimation** - age of the person from the face using [MiVOLO](https://github.com/WildChlamydia/MiVOLO) (v2 model),
   compared with a declared age / date of birth when one is supplied with the upload. A mismatch goes to human review.

4. **Face match** - the face in the video against the photo on the ID document, by a *standalone service* (YuNet + SFace).
5. **Liveness** - a passive anti-spoofing *risk signal* from a second standalone service (MiniFASNet); not proof.

Uncertain results go to a human reviewer. React (Vite) frontend + FastAPI backend. Speech and information-consistency from the SOW are
not part of this build yet. The dashboard and the report are generated from a
registry of tests (`app/parameters.py`), so each new test appears in them after one registry entry plus its analyzer.

## Run locally
```bash
# 1. put the DFSpot checkpoints in backend/models/  (see below) - the app refuses uploads until they exist
# 2. backend  (http://localhost:8000, docs at /docs)
cd backend && python -m venv .venv && .venv/Scripts/activate   # source .venv/bin/activate on mac/linux
pip install -r requirements.txt
uvicorn app.main:app
# 3. frontend (http://localhost:5173, proxies /api to :8000)
cd frontend && npm install && npm run dev
```
Tests: `cd backend && pytest` (uses a stub detector, no weights needed).

## Checkpoints
Download only from sources you trust (see DeepFakeSol's README; they are several GB). Required file names:
```
backend/models/dfdc_v2.pth   dfdc_v2st.pth   dfdc_vit.pth   dfdc_vitst.pth      (or ffpp_* / celeb_*)
```
`AUTHENTICITY_MODELS` (default `["TimmV2"]`, the cheapest on CPU) lists which networks run; each needs its file.
Checkpoints are loaded with `torch.load(weights_only=True)`. `GET /api/health` reports `degraded` and uploads return
503 until the listed checkpoints are present.

## How a video is scored
Sample `AUTHENTICITY_FPV` (32) frames -> BlazeFace face crops -> each model scores each face -> per-model score =
sigmoid(mean logit) -> ensemble fake score = mean of models.

| Fake score | Verdict | Decision |
|---|---|---|
| `< 0.15` | Pass | Accept (if confidence >= 0.75) |
| `0.15 - 0.30` | Review | Needs Review |
| `>= 0.30` | Suspected Manipulation | Reject (if confidence >= 0.75, else Needs Review) |
| fewer than 8 frames with a face, decode error | Review | Needs Review |

Confidence rises with distance past the cutoff and falls when ensemble models disagree, so a marginal flag is routed
to a person instead of being auto-rejected. Thresholds are editable at runtime (UI: Rules & thresholds, API: `/api/rules`; `/api/config` still works).
**Scores are not calibrated probabilities** - the cutoffs are DeepFakeSol's defaults and must be validated on labelled
Bajaj Life data before the decisions are relied on.

## What has and hasn't been verified
- Verified: upload validation, queueing, face detection, preprocessing, model loading, inference, aggregation, decision
  rules, review workflow, dashboard aggregates (automated tests + an end-to-end run with a random-weight checkpoint on a real
  face clip: 16 frames in about 6 s on CPU).
- **Not verified: detection accuracy with the real checkpoints** (no weights or labelled videos were available).
  Do this before go-live: run known-real and known-fake clips, compute false-positive / false-negative rates, set thresholds.
- Docker files are provided but have not been run (the Linux image also needs the Microsoft SQL driver's system libraries; untested).

## Standalone services: Face match and Liveness
`services/face_match` and `services/liveness` are **independent FastAPI services** (own `requirements.txt`, Dockerfile, tests, README and
API keys). The main app calls them over HTTP, so they can be deployed, scaled, replaced or called by other systems on their own.

| | Face match (`:8101`) | Liveness (`:8102`) |
|---|---|---|
| Models | YuNet + SFace (OpenCV Zoo), CPU | YuNet + 2x MiniFASNet (Minivision, Apache-2.0), `onnxruntime`, CPU |
| Endpoints | `/v1/faces/detect`, `/compare`, `/compare-video` | `/v1/liveness/image`, `/video` |
| Auth | `X-API-Key` (`FACE_MATCH_API_KEYS`); refuses to start without a key | `X-API-Key` (`LIVENESS_API_KEYS`) |

Run each (see its README for details):
```bash
cd services/face_match && python -m venv ../.venv && ../.venv/Scripts/pip install -r requirements.txt && ../.venv/Scripts/python scripts/download_models.py
FACE_MATCH_API_KEYS=<key> ../.venv/Scripts/python -m uvicorn app.main:app --port 8101
cd ../liveness && ../.venv/Scripts/pip install -r requirements.txt && ../.venv/Scripts/python scripts/download_models.py
LIVENESS_API_KEYS=<key> ../.venv/Scripts/python -m uvicorn app.main:app --port 8102
```
Then set `FACE_MATCH_URL`, `FACE_MATCH_API_KEY`, `LIVENESS_URL`, `LIVENESS_API_KEY` in `backend/.env` (`.env.example`). If a service is not
configured or down, its test is reported as **Unavailable** and everything else still runs (the dashboard shows a banner).

- **ID document photo:** optional on upload (one image, applied to every video in that upload; JPG/PNG/BMP/WebP, 10 MB). It is personal data:
  it is stored with the video (and moves to blob storage with it), is shown to reviewers and administrators next to the face-match result
  (`GET /api/cases/{id}/id-document`, never to viewers, `Cache-Control: no-store`), and is deleted with the video.
- **Face match result:** `Match` / `Review` / `Mismatch` / `Not checked` (no ID photo) / `Unavailable`. A mismatch or doubt turns an Accept into
  Needs Review (`face_mismatch_requires_review`, on by default); it never rejects. Poor-quality images can never produce a Mismatch.
  The two similarity thresholds (default 0.45 / 0.28, **uncalibrated**) are on the Rules & thresholds page and sent to the service on every call.
- **Liveness result:** `Pass` / `Review` (suspected attack, or unclear) / `Low confidence` (too few judgeable frames, common for webcam close-ups) /
  `Unavailable`. Only a *suspected attack* routes to review (`liveness_suspected_requires_review`); it never rejects.
- **Measured:** face match separates same-person from different-person well on easy pairs (median 0.84 vs at most 0.23); the liveness crop code and ONNX
  conversion are verified equal to upstream; real services were exercised end to end (same person -> Match / Accept, other person -> Mismatch / Needs Review).
- **Not measured, and important:** ID-photo-versus-video accuracy (harder than the easy pairs used here); and **spoof detection**: no real attack
  samples were available, a crude synthetic print was not caught, genuine frames occasionally score as spoof, and the model does not detect
  deepfakes (the video-authenticity test does). Treat liveness as a weak risk signal. For real liveness assurance use a capture-time, challenge-response check.
- Face matching and liveness are biometric processing of customers: consent and DPDP / IRDAI review are needed. Confirm model licences against the upstream repositories.

## Health indicators (open-rppg)
Remote photoplethysmography reads the tiny colour changes of the pulse in the face. Per video we report heart rate,
breathing rate, HRV (SDNN, RMSSD, pNN50, LF/HF, when the clip is long and clean enough), the **signal-quality index (SQI)**,
the video's frame statistics and a downsampled pulse waveform. Model: `FacePhys.rlap` (`VITALS_MODEL`).
- **Status:** `Normal range` / `Flagged` (reliable reading outside the configured ranges) / `Low signal quality`
  (SQI < 0.5, video < 10 s, or no pulse found - numbers are shown but never trusted or flagged) / `Unavailable` (error).
- **Decision:** informational by default. Switch on *Send a video with a flagged vital sign to manual review* on the
  Thresholds tab and a Flagged reading turns an Accept into Needs Review. Health never causes a Reject, and an unreliable
  reading never changes the decision. The SOW says health indicators are subject to business rules and human review;
  those rules are not defined yet, so the ranges (HR 50-100 bpm, breathing 8-25/min) are placeholders.
- **API:** the test runs automatically on every upload and appears in the case report (`parameters.health_indicators`).
  `POST /api/vitals/analyze` (multipart `file`) runs only this test on one video and returns the result immediately
  (nothing stored; same upload limits). `GET /api/health` reports whether it is ready.
- **Install:** `pip install --no-deps open-rppg==0.1.1` after `requirements.txt` (it declares full `opencv-python`, which
  would clash with `opencv-python-headless`). Weights ship inside the package. First use loads the model (~25 s).
- **Verified:** automated tests (stubbed model) and real runs. On a face with a synthetic pulse of known rate in intra-frame
  video it recovered 72 -> 72.0 bpm and 110 -> 110.25 bpm with SQI ~0.9 and HRV. On the real (compressed) videos available
  here it reported **low signal quality** (SQI 0.1-0.4), and the same clip gave different heart rates on repeated runs.
- **Not verified / caveats:** accuracy on real PIVC recordings, which are normally compressed. The library itself warns that
  videos with non-key frames (ordinary H.264/MPEG-4 video) damage the signal and recommends key-frame-only recording.
  These are research-grade estimates, not medical measurements. **Licensing:** the code is MIT, but the pretrained
  weights belong to their original papers/datasets and carry their own terms - get legal sign-off before commercial or
  underwriting use. Using health data for underwriting also needs customer consent and regulatory review (DPDP, IRDAI).

## Age estimation (MiVOLO)
MiVOLO v2 (multi-input transformer, face + body) estimates age and gender; we use **age only**. Per video: 8 frames are
sampled (`AGE_FRAMES`), YOLOv8 finds faces and bodies, MiVOLO estimates age for the largest face in each frame, and we report
the median, the typical range (10th-90th percentile), the spread across frames and how many frames were usable.
- **Status:** `Estimated age` (reliable; consistent with the declared age if one was given) / `Review` (reliable but further
  than the tolerance from the declared age) / `Low confidence` (too few usable frames or estimates vary too much - shown,
  never treated as a mismatch) / `Unavailable` (error).
- **Declared age:** optional on upload (`declared_age` or `dob`). Without it the age is reported for information only.
- **Decision:** a `Review` (mismatch) turns an Accept into Needs Review (rule `age_mismatch_requires_review`, on by default;
  tolerance 7 years is a placeholder). It never causes a Reject. Authenticity still decides Reject.
- **Isolated environment:** MiVOLO pins `timm==0.8.13.dev0` and `ultralytics==8.1.0`, which conflict with the main app, so it
  runs as a separate worker process in its own virtualenv (`backend/.venv-age`, code in `backend/age_worker/`, MiVOLO source
  vendored under Apache-2.0). The app starts it lazily, talks to it over stdin/stdout, and restarts it if it dies or times out.
  ```bash
  python -m venv backend/.venv-age
  backend/.venv-age/Scripts/python -m pip install -r backend/age_worker/requirements.txt   # bin/python on Linux/macOS
  backend/.venv-age/Scripts/python backend/scripts/download_age_models.py                   # ~240 MB into backend/models/age/
  ```
  The download script pins the Hugging Face revisions that were reviewed and writes SHA-256 checksums to
  `models/age/MANIFEST.json`. If the environment or models are missing the test is reported as Unavailable and nothing else is affected.
- **API:** runs automatically on upload (`parameters.age_estimation` in the report). `POST /api/age/analyze` (multipart `file`,
  optional `declared_age` / `dob`) runs only this test and returns the result (nothing stored).
- **Speed:** about 2 s per sampled frame on CPU (about 15-25 s per video at 8 frames, plus a one-time model load); a GPU is far faster.
- **Verified:** automated tests (stubbed worker) and real runs of the real worker: on five real clips it found the face in every
  sampled frame and produced consistent per-frame estimates (spread 0.7-2.5 years; ages 31-56). **Accuracy has not been measured**
  - there was no ground-truth age for these clips. Published error is roughly 3.7-4.2 years MAE on benchmark photos; compressed video,
  low light, masks, glasses, heavy makeup or filters will be worse. Calibrate the tolerance on real PIVC videos with known ages.
- **Licensing (get legal sign-off before commercial use):** MiVOLO code is Apache-2.0 (its setup metadata says
  CC BY-SA 4.0 - inconsistent, ask the authors); the v2 weights on Hugging Face are Apache-2.0 but were trained on a mix of
  "proprietary and open-source datasets"; the person/face **detector weights and ultralytics are AGPL-3.0** (network copyleft)
  - running it as a separate process does not by itself resolve AGPL obligations for a hosted service. The detector `.pt`
  is a pickle file, so only use the pinned, checksummed download. Age/biometric estimation of customers also needs consent
  and regulatory review (DPDP, IRDAI).

## Users and roles
The whole app (every API route except `/api/health` and the sign-in routes) requires a signed-in user. **User management**
(left menu, administrators only) creates accounts and assigns roles:

| | Viewer | Reviewer | Administrator |
|---|:-:|:-:|:-:|
| See dashboard, videos, reports, play video | yes | yes | yes |
| Upload videos, re-run tests, standalone test APIs | | yes | yes |
| Record review decisions (Accept / Reject) | | yes | yes |
| Delete videos | | | yes |
| Change thresholds and connections | | | yes |
| Manage users | | | yes |

- **First run:** with no users the sign-in page becomes "Create the administrator account". For deployments, set
  `ADMIN_USERNAME` / `ADMIN_PASSWORD` instead so the account exists at start-up (`backend/.env.example`).
- **Accounts:** an administrator adds a user with a temporary password (the page can generate one and shows it once). The user
  must choose their own password at first sign-in and after an administrator resets it. Admins can edit role/name, deactivate
  (ends their sessions), unlock, reset the password and delete. There is always at least one active administrator, and you cannot
  demote, deactivate or delete yourself.
- **Security:** passwords are hashed with scrypt (at least 10 characters, not containing the username); sessions are random
  tokens in an `HttpOnly`, `SameSite=Lax` cookie, stored hashed on the server, valid 12 h; an account locks for 15 minutes after
  5 wrong passwords; sign-in errors do not reveal whether a username exists. Reviews, uploads, re-runs and deletions are recorded
  under the signed-in username (the old self-typed reviewer name and `ADMIN_TOKEN` are gone). Permissions are enforced by the
  server; the menu only hides what a role cannot use.
- **Not included:** SSO / MFA / password-reset by e-mail, and per-case assignment of reviewers.
- **Upgrading an existing install:** nothing is migrated; the first visit shows the setup screen. Older reviews keep the reviewer
  name they were recorded with.

## Selecting videos: re-run and delete
**All videos** and the **Review queue** have a checkbox on every row (and a select-all for the page). Ticking anything shows an
action bar:
- **Re-run tests** queues the selected videos for a fresh analysis with the current thresholds and models. A completed human
  review is reset, but the previous AI/final decision and reviewer are kept in the case's audit trail ("re-run requested").
- **Delete** permanently removes the videos: results, review history, the stored file(s) and, if the video lives in Azure Blob
  Storage, the blobs and the empty case folder (storage accounts with a hierarchical namespace leave an empty folder object
  behind; it is removed only if it really is an empty marker). If a blob cannot be deleted (e.g. missing permission) that video
  is kept and the error is shown, so nothing is silently orphaned. Videos being analysed at that moment are skipped.
  A small `deletion_log` row (case, file name, outcome, who, when) is kept. Only administrators can delete.
- The selection is cleared when you change the search/filter, so a hidden selection can never be deleted by accident.
- API: `POST /api/cases/bulk` with `{"action": "delete" | "rerun", "ids": [...]}` (max 500) returns a per-video result.

## Configuration page: connections
**Configuration** has one tab per connection type - Azure Blob Storage, Azure API, Omni DB, Other - plus Thresholds.
Each tab has a form on top and the **saved connections below** (edit, test, activate/deactivate, delete). Secrets
(connection strings, keys, passwords) are encrypted with `APP_SECRET_KEY`, never returned by the API, and kept
when you edit a connection and leave the secret blank. One connection per type is *active*.
- **Azure Blob Storage** is wired in: new uploads are staged on the server, analysed, then the original and its
  browser-playable preview are uploaded to `<container>/<prefix><case id>/video.<ext>` and `preview.mp4`, and the
  local copy is deleted **only after the upload succeeded** (on failure it is kept and the reason appears in the
  case audit trail). Playback and re-analysis read from a 24 h local cache. Videos stored before blob storage was
  enabled stay on disk. Deactivating the connection sends new uploads back to local disk. Auth: connection string
  or managed identity (needs "Storage Blob Data Contributor").
- **Azure API, Omni DB, Other** are stored for the upcoming integrations and marked "not used yet".
- **Test connection** never saves anything. Blob: write/read/delete a test blob. Azure API / Other: an HTTPS
  reachability probe (https only; private, loopback and link-local addresses are refused). Omni DB: `SELECT 1`
  (SQL Server and PostgreSQL work; there is no MySQL driver installed).
- Only administrators can see or change connections (see *Users and roles*).
- Verified: automated tests, and the blob flow end to end against the Azurite emulator and a connection test against
  a real storage account. Not yet verified: a full upload to a real account, managed identity.

## Deploy
`docker compose up --build` (set `SQL_CONNECTION_STRING`; mount checkpoints at `backend/models`). Notes:
- Put TLS in front (the session cookie is marked `Secure` automatically behind HTTPS / `X-Forwarded-Proto: https`).
  Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` so the first administrator exists before the app is reachable; otherwise the
  first visitor sees a setup screen and can claim the admin account.
- Run **one backend process per replica** (the analysis queue and models are in-process); `WORKER_THREADS` controls
  concurrency. Use a GPU image for volume: CPU is roughly 6 s per video per model.
- Uploads are stored on disk under `STORAGE_DIR`; use a persistent volume. There is no retention/deletion policy yet.
- The tables are created (and new columns added) at startup in the schema named by `SQL_SCHEMA`; see *Database*.

## Remaining before this is production-grade
Single sign-on (SSO/MFA) instead of local passwords, retention and deletion of videos, Alembic migrations, object storage instead of local disk,
a durable job queue (Celery/RQ/SQS) for multi-node, monitoring/alerting, threshold calibration on real data, and the
other six SOW parameters.

## API
`POST /api/cases/upload` (multipart `files`, optional `reference`, `declared_age`, `dob`, `id_document`) - `GET /api/cases` (filters: `status`, `decision`,
`review_status`, `q`, `parameter`/`parameter_status`, `days`, `sort`) - `GET /api/cases/{id}` - `GET /api/cases/{id}/video` -
`POST /api/cases/{id}/review` - `POST /api/cases/{id}/analyze` - `GET /api/dashboard?days=` - `GET/PUT /api/config` -
`GET /api/reports/export.csv` - `GET /api/cases/{id}/report` - `POST /api/vitals/analyze` - `POST /api/age/analyze` - `GET /api/cases/{id}/id-document` - `GET /api/health`. The standalone services have their own APIs (`/docs` on ports 8101 / 8102).


## Audit log, reports, rules and service health

- **Audit log** (`/audit`, reviewers and admins): who uploaded, decided, re-ran, deleted, signed in, exported, or changed a rule, connection or user, and when.
  Searchable, filterable, exportable. Rows have no foreign keys, so they outlive the video or user they describe. Secrets and passwords are never written.
  History from before this existed is copied in on first start.
- **Reports** (`/reports`, reviewers and admins): date range, decisions, review state and per-test results, a column choice, then CSV (complete, streamed) or PDF
  (summary + first 5,000 rows). Every export is recorded in the audit log. Cells starting with `= + - @` are neutralised against spreadsheet formula injection.
- **Rules & thresholds** (`/rules`, admins): every save is a new version with who/why; restore any version. Editing shows **what would change**: past videos are
  re-judged from their stored measurements (no model re-run) under the live and proposed rules, with flips, per-test changes and agreement with what reviewers
  decided. It examines at most the newest 20,000 videos in the window and reports a method check (how often a replay reproduces the stored decision).
- **Service health** (`/health`, admins): per engine, readiness, version, and median / slowest-5% time and failure rate from real runs (table `engine_calls`,
  kept 30 days), plus a live check for the two services. Existing databases get the new columns automatically at start-up (`app/migrate.py`).


## AI interpretation and PDF reports
- **Set up:** Configuration -> **OpenAI** tab: OpenAI (model + API key, optional https base URL) or Azure OpenAI (endpoint, deployment name, API version, key).
  The key is encrypted like other secrets and never shown again. *Test connection* makes one tiny request. Only the active connection is used.
- **Interpretation:** on a video's page, *Write an interpretation* (reviewers and admins) gives a plain-language explanation: headline, summary, a finding per test,
  what to look at, suggested next steps and what the results cannot tell. It is advisory only: it never changes the decision and is stored with the video.
  If a test is re-run afterwards it is marked out of date until updated.
- **What the model sees:** only each test's status, scores, reasons and the thresholds in force. Never the video, the ID photo, file name, reference, customer
  details, user names, or engine/model names. The prompt tells it to treat all of that as data, not instructions. Output is validated and size-limited.
  Aggregate overviews (below) get counts only. Every generation is in the audit log (without its text).
- **PDF of one video:** *Download PDF report* on the video page: decision and review, the interpretation (if written), results at a glance, each test with its readings,
  reasons and limits (plus a per-frame score chart for authenticity), the human review, rules in force and the activity trail. Anyone who can open the video can download it.
- **Reports page:** a PDF can include an AI-written overview of the counts (tick *Add a written overview by AI*). If AI is not set up or the service fails, the download says so rather than silently leaving it out.
- Nothing here has been run against the real OpenAI service (tests use a stand-in); do a test with your key before relying on it, and check your data-handling terms with the provider.

## History of test runs
`/history` (everyone who can sign in) lists every analysis that was requested: the first one after an upload, a re-run of everything, and a
re-run of just some tests (for example only Age estimation). Runs that are waiting or running are shown first (with their place in line
and a live timer), then finished ones with each test's result before and after and any change to the overall decision. It speaks in test
names only; no engine or model names. Failure details are shown to administrators only. Table `runs`; a video's runs stay listed after it is deleted.

## Database (Azure SQL)
Everything the app stores (users and sessions, connections, rules and their versions, videos and test results, the audit log,
engine-health figures) lives in **Azure SQL**, in its own schema (`SQL_SCHEMA`, default `pivc`), so it never touches other
applications in the same database. The code talks to it directly with Microsoft's `mssql-python` driver in plain T-SQL (no ORM:
`app/db.py`, `app/schema.py`).

- **Configure:** `SQL_CONNECTION_STRING` in `backend/.env` (the ADO.NET string from the Azure portal works as it is). Keep it out of git.
  Rotate the password if it has ever been pasted into a chat or ticket.
- **First start** creates the schema, tables and indexes; later starts add any new columns. No foreign keys: the app deletes a
  video's rows itself, and the audit log deliberately outlives what it describes.
- **Move existing local data** (the old `pivc.db`): `python scripts/import_sqlite.py pivc.db` (refuses if Azure SQL already has data;
  add `--sessions` to keep sign-ins). Passwords and encrypted secrets are copied unchanged, so keep the same `APP_SECRET_KEY`.
- **Tests** run in a separate schema, `pivc_test` (set by `tests/conftest.py`), and the functions that empty tables refuse to run
  on any schema that does not end in `_test`. The tests need network access to the database.
- Needs the server firewall to allow this machine's IP, and a login that may create tables in the schema.
