from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Azure SQL (all application data). The ADO.NET string from the Azure portal works as it is. Tables are created in their own schema.
    sql_connection_string: str = ""
    sql_schema: str = "pivc"
    sql_pool_size: int = 8
    storage_dir: str = "./storage"
    max_upload_mb: int = 500
    allowed_video_extensions: list[str] = [".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv"]

    # Security. APP_SECRET_KEY encrypts secrets saved from the UI. ADMIN_USERNAME/ADMIN_PASSWORD create the first
    # administrator on start-up when there are no users yet (otherwise the first visitor sees a setup screen).
    app_secret_key: str = ""
    admin_username: str = ""
    admin_password: str = ""
    azure_storage_connection_string: str = ""  # alternative to saving it in the UI

    # Video authenticity (DFSpot / DeepFakeSol)
    authenticity_models_dir: str = "./models"
    authenticity_dataset: str = "dfdc"  # dfdc | ffpp | celeb (selects the checkpoint family)
    authenticity_models: list[str] = ["TimmV2"]  # any of TimmV2 TimmV2ST ViT ViTST
    authenticity_fpv: int = 32  # frames sampled per video
    # Vital signs from face video (open-rppg). Estimates only - see app/vitals/analyzer.py
    vitals_enabled: bool = True
    vitals_model: str = "FacePhys.rlap"
    # Age estimation (MiVOLO) runs in its own virtualenv/process: see app/age/analyzer.py
    age_enabled: bool = True
    age_python: str = ""  # default: backend/.venv-age
    age_models_dir: str = "./models/age"
    age_frames: int = 8  # frames sampled per video (CPU: ~2 s per frame)
    # Standalone services (services/face_match and services/liveness): base URL + API key. Empty URL = not configured.
    face_match_enabled: bool = True
    face_match_url: str = ""
    face_match_api_key: str = ""
    face_match_frames: int = 12
    liveness_enabled: bool = True
    liveness_url: str = ""
    liveness_api_key: str = ""
    liveness_frames: int = 12
    services_timeout_s: int = 300
    max_id_document_mb: int = 10
    allowed_image_extensions: list[str] = [".jpg", ".jpeg", ".png", ".bmp", ".webp"]
    worker_threads: int = 1  # analysis workers (model inference is heavy; keep low)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


settings = Settings()

# Business rules / thresholds, editable at runtime through /api/config. Scores are not calibrated
# probabilities, so these must be validated on labelled Bajaj Life data during POC/UAT.
DEFAULT_RULES: dict = {
    "review_sla_hours": 24,             # a video waiting longer than this for a human decision is "over SLA" (dashboard)
    "confidence_threshold": 0.75,       # below this -> Needs Review (never auto-decided)
    "fake_score_review": 0.15,          # ensemble fake score >= this -> Review (below -> Pass)
    "fake_score_flag": 0.30,            # >= this -> Suspected Manipulation (DeepFakeSol default cutoff)
    "min_face_frames": 8,               # fewer frames with a detected face -> Review
    "bg_blur_ratio_max": 0.30,          # background sharpness / face sharpness below this -> blurred background
    "bg_static_diff_max": 0.25,         # mean frame-to-frame background change below this -> static/replaced
    "virtual_bg_blocks_auto_reject": True,  # manipulation flag on a virtual background goes to a human
    # Health indicators (rPPG vitals). Informational unless health_flag_requires_review is switched on.
    "health_sqi_min": 0.5,              # signal quality below this -> "Low signal quality", values not trusted
    "health_min_seconds": 10,           # shorter videos cannot give a reliable reading
    "hr_normal_low": 50,                # resting heart rate range (bpm); outside -> Flagged
    "hr_normal_high": 100,
    "resp_normal_low": 8,               # breathing rate range (breaths/min)
    "resp_normal_high": 25,
    "health_flag_requires_review": False,  # a Flagged reading turns an Accept into Needs Review
    # Age estimation. Compared with the declared age/DOB when one is supplied with the upload.
    "age_tolerance_years": 7,           # |estimated - declared| above this -> Review
    "age_min_frames": 5,                # fewer frames with an age estimate -> Low confidence
    "age_max_spread": 10,               # std-dev of per-frame ages above this (years) -> Low confidence
    "age_mismatch_requires_review": True,  # a mismatch turns an Accept into Needs Review (never a Reject)
    # Face match against the ID document photo (service: services/face_match). Thresholds are sent to the service on every call.
    "face_match_threshold": 0.45,       # median cosine similarity at/above this (and most frames matching) -> Match
    "face_mismatch_threshold": 0.28,    # below this on good-quality images -> Mismatch; in between -> Review
    "face_mismatch_requires_review": True,  # Mismatch or Review turns an Accept into Needs Review (never a Reject)
    # Liveness (service: services/liveness). A passive risk signal, not proof: see services/liveness/README.md
    "liveness_pass_threshold": 0.80,    # median real-face probability at/above this -> Pass
    "liveness_spoof_threshold": 0.30,   # consistently below this on judgeable frames -> possible presentation attack
    "liveness_suspected_requires_review": True,  # a suspected attack turns an Accept into Needs Review (never a Reject)
}
