import logging
from pathlib import Path

from ..authenticity.background import analyze_background
from ..authenticity.detector import DetectorUnavailable, get_detector
from ..config import settings
from ..age.analyzer import AgeUnavailable, get_analyzer as get_age_analyzer
from ..vitals.analyzer import VitalsUnavailable, get_analyzer
from .. import services_client as sc
from .. import engines
from .base import Context

log = logging.getLogger(__name__)


class Provider:
    """Runs the real models. Other parameters (face match, liveness, ...) will be added here later."""

    name = "dfspot"
    engines = {"video_authenticity": "dfspot", "health_indicators": "open-rppg", "age_estimation": "mivolo",
               "face_match": "sface", "liveness": "minifasnet"}

    def extract(self, ctx: Context, only: list[str] | None = None) -> dict:
        """Run the engines. `only` (test keys) limits it to those engines; each one is independent of the others."""
        obs: dict = {"engines": self.engines}
        steps = {"video_authenticity": self._authenticity, "health_indicators": self._vitals, "age_estimation": self._age,
                 "face_match": self._face_match, "liveness": self._liveness}
        for key, step in steps.items():
            if only is None or key in only:
                step(ctx, obs)
        return obs

    def _vitals(self, ctx: Context, obs: dict) -> None:
        """Independent of the authenticity test: a failure here never affects it."""
        try:
            if not settings.vitals_enabled:
                raise ValueError("Vital-sign analysis is disabled")
            if not ctx.media.get("video"):
                raise ValueError("No video file available for this case")
            with engines.track("health_indicators"):
                obs["vitals_detail"] = get_analyzer().analyze(ctx.media["video"])
        except (VitalsUnavailable, ValueError, OSError, RuntimeError) as e:
            log.warning("vitals failed for %s: %s", ctx.case_ref, e)
            obs["vitals_detail"] = {"error": str(e)}

    def _face_match(self, ctx: Context, obs: dict) -> None:
        """Video frames against the ID document photo, via the standalone Face Match service."""
        try:
            if ctx.media.get("id_document_error"):
                raise ValueError(ctx.media["id_document_error"])
            ref = ctx.media.get("id_document")
            if not ref:
                obs["face_detail"] = {"skipped": "No ID document photo was provided with this video."}
                return
            with engines.track("face_match"):
                st = get_face_match_client().status()
                if not st["ready"]:
                    raise ValueError(f"Face Match service is not available: {st['reason']}")
                obs["face_detail"] = sc.compare_video(Path(ctx.media["video"]), Path(ref), ctx.rules)
        except (sc.ServiceError, ValueError, OSError) as e:
            log.warning("face match failed for %s: %s", ctx.case_ref, e)
            obs["face_detail"] = {"error": str(e)}

    def _liveness(self, ctx: Context, obs: dict) -> None:
        """Passive anti-spoofing on sampled video frames, via the standalone Liveness service."""
        try:
            with engines.track("liveness"):
                st = get_liveness_client().status()
                if not st["ready"]:
                    raise ValueError(f"Liveness service is not available: {st['reason']}")
                obs["liveness_detail"] = sc.check_liveness(Path(ctx.media["video"]), ctx.rules)
        except (sc.ServiceError, ValueError, OSError, KeyError) as e:
            log.warning("liveness failed for %s: %s", ctx.case_ref, e)
            obs["liveness_detail"] = {"error": str(e)}

    def _age(self, ctx: Context, obs: dict) -> None:
        """Independent of the other tests: a failure here never affects them."""
        try:
            if not settings.age_enabled:
                raise ValueError("Age estimation is disabled")
            if not ctx.media.get("video"):
                raise ValueError("No video file available for this case")
            with engines.track("age_estimation"):
                obs["age_detail"] = get_age_analyzer().analyze(ctx.media["video"])
        except (AgeUnavailable, ValueError, OSError, RuntimeError) as e:
            log.warning("age estimation failed for %s: %s", ctx.case_ref, e)
            obs["age_detail"] = {"error": str(e)}

    def _authenticity(self, ctx: Context, obs: dict) -> None:
        try:
            video = ctx.media.get("video")
            if not video:
                raise ValueError("No video file available for this case")
            with engines.track("video_authenticity"):
                det = get_detector().analyze(video)
            items = det.pop("_bg_items", [])
            det["background"] = analyze_background(items, ctx.rules)
            obs["authenticity_detail"] = det
            obs["authenticity_score"] = None if det["ensemble_score"] is None else 1 - det["ensemble_score"]
        except (DetectorUnavailable, ValueError, OSError, RuntimeError) as e:
            log.warning("authenticity failed for %s: %s", ctx.case_ref, e)
            obs["authenticity_detail"] = {"error": str(e)}
            obs["authenticity_score"] = None


get_face_match_client = sc.face_match_client   # module-level names so tests can substitute them
get_liveness_client = sc.liveness_client


def get_provider() -> Provider:
    return Provider()
