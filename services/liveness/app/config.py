from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """All settings come from LIVENESS_* environment variables (or a .env file)."""
    model_config = SettingsConfigDict(env_prefix="LIVENESS_", env_file=".env", extra="ignore")

    # --- security: callers send one of these keys in the X-API-Key header ---
    api_keys: str = ""                 # comma separated, so keys can be rotated without downtime
    disable_auth: bool = False         # local development only; the service refuses to start with no keys otherwise

    # --- models ---
    models_dir: Path = ROOT / "models"

    # --- limits ---
    max_image_mb: int = 10
    max_video_mb: int = 500
    max_pixels: int = 40_000_000
    default_frames: int = 12
    max_frames: int = 60

    # --- detection / framing ---
    det_score_threshold: float = 0.7
    min_face_px: int = 60              # smaller faces are flagged "too_small" (the models see an 80x80 crop of the face area)

    # --- decision. UNCALIBRATED defaults: measure on your own genuine and attack recordings. ---
    pass_threshold: float = 0.80       # median probability of "real" at or above this (and most frames real) -> pass
    spoof_threshold: float = 0.30      # median below this -> spoof_suspected
    min_usable_frames: int = 3

    @field_validator("api_keys")
    @classmethod
    def _strip(cls, v: str) -> str:
        return ",".join(k.strip() for k in v.split(",") if k.strip())

    @property
    def keys(self) -> list[str]:
        return [k for k in self.api_keys.split(",") if k]


def get_settings() -> Settings:
    return Settings()
