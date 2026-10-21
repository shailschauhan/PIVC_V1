from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """All settings come from FACE_MATCH_* environment variables (or a .env file)."""
    model_config = SettingsConfigDict(env_prefix="FACE_MATCH_", env_file=".env", extra="ignore")

    # --- security: callers send one of these keys in the X-API-Key header ---
    api_keys: str = ""                 # comma separated, so keys can be rotated without downtime
    disable_auth: bool = False         # local development only; the service refuses to start with no keys otherwise

    # --- models ---
    models_dir: Path = ROOT / "models"

    # --- limits ---
    max_image_mb: int = 10
    max_video_mb: int = 500
    max_pixels: int = 40_000_000       # decompression-bomb guard for images
    default_frames: int = 12
    max_frames: int = 60

    # --- detection ---
    det_score_threshold: float = 0.7
    min_face_px: int = 60              # shorter side of the face box; smaller faces are flagged "too_small"
    min_sharpness: float = 25.0        # variance of the Laplacian on the 112x112 aligned face; lower is flagged "blurry"
    max_yaw: float = 0.45              # |nose offset| / eye distance; larger is flagged "off_angle" (profile view)

    # --- decision bands on cosine similarity. UNCALIBRATED defaults: measure on your own ID-photo/selfie pairs. ---
    match_threshold: float = 0.45
    mismatch_threshold: float = 0.28
    min_usable_frames: int = 3         # video: fewer good frames than this -> result is flagged low quality

    @field_validator("api_keys")
    @classmethod
    def _strip(cls, v: str) -> str:
        return ",".join(k.strip() for k in v.split(",") if k.strip())

    @property
    def keys(self) -> list[str]:
        return [k for k in self.api_keys.split(",") if k]


def get_settings() -> Settings:
    return Settings()
