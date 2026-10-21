"""Registry of the tests (parameters) the solution runs on every video.

The dashboard, drill-down links and per-test cards are generated from this list, so adding the next test from the SOW
(face match, liveness, speech, ...) is: write its analyzer in analyzers/checks.py, add it to ANALYZERS, and add one entry here.
"""
from typing import Callable

# status -> tone used for colour in the UI: good | warn | bad | muted
PARAMETERS: list[dict] = [
    {
        "key": "video_authenticity", "label": "Video authenticity", "engine": "DFSpot ensemble",
        "question": "Is the video AI-generated or manipulated?",
        "outcomes": [("Pass", "good"), ("Review", "warn"), ("Suspected Manipulation", "bad")],
        "headline": {"label": "Flagged or inconclusive", "statuses": ["Review", "Suspected Manipulation"]},
        "distribution": {"label": "Fake score per video", "unit": "", "lo": 0.0, "hi": 1.0, "bins": 10,
                         "markers": [("fake_score_review", "review"), ("fake_score_flag", "flag")]},
        "status": "app.authenticity.detector:engine_status",
    },
    {
        "key": "health_indicators", "label": "Health indicators", "engine": "open-rppg",
        "question": "Are heart rate and breathing readable and within range?",
        "outcomes": [("Normal range", "good"), ("Flagged", "warn"), ("Low signal quality", "muted"), ("Unavailable", "muted")],
        "headline": {"label": "Measured reliably", "statuses": ["Normal range", "Flagged"]},
        "distribution": {"label": "Heart rate (bpm)", "unit": "bpm", "lo": 40.0, "hi": 140.0, "bins": 10,
                         "markers": [("hr_normal_low", "low"), ("hr_normal_high", "high")]},
        "status": "app.vitals.analyzer:engine_status",
    },
    {
        "key": "age_estimation", "label": "Age estimation", "engine": "MiVOLO v2",
        "question": "Does the face look consistent with the declared age?",
        "outcomes": [("Estimated age", "good"), ("Review", "warn"), ("Low confidence", "muted"), ("Unavailable", "muted")],
        "headline": {"label": "Estimated reliably", "statuses": ["Estimated age", "Review"]},
        "distribution": {"label": "Estimated age (years)", "unit": "yrs", "lo": 0.0, "hi": 100.0, "bins": 10, "markers": []},
        "status": "app.age.analyzer:engine_status",
    },
    {
        "key": "face_match", "label": "Face match", "engine": "SFace (OpenCV)",
        "question": "Is the person in the video the person on the ID document?",
        "outcomes": [("Match", "good"), ("Review", "warn"), ("Mismatch", "bad"), ("Not checked", "muted"), ("Unavailable", "muted")],
        "headline": {"label": "Needs attention", "statuses": ["Review", "Mismatch"]},
        "distribution": {"label": "Median similarity to the ID photo", "unit": "", "lo": 0.0, "hi": 1.0, "bins": 10,
                         "markers": [("face_mismatch_threshold", "mismatch"), ("face_match_threshold", "match")]},
        "status": "app.services_client:face_match_status",
    },
    {
        "key": "liveness", "label": "Liveness", "engine": "MiniFASNet (risk signal)",
        "question": "Does the face look like a real, present person rather than a photo or screen?",
        "outcomes": [("Pass", "good"), ("Review", "warn"), ("Low confidence", "muted"), ("Unavailable", "muted")],
        "headline": {"label": "Judged reliably", "statuses": ["Pass", "Review"]},
        "distribution": {"label": "Median real-face probability", "unit": "", "lo": 0.0, "hi": 1.0, "bins": 10,
                         "markers": [("liveness_spoof_threshold", "suspect"), ("liveness_pass_threshold", "pass")]},
        "status": "app.services_client:liveness_status",
    },
]
BY_KEY = {p["key"]: p for p in PARAMETERS}


def engine_status_fn(spec: dict) -> Callable[[], dict]:
    module, fn = spec["status"].split(":")
    import importlib
    return getattr(importlib.import_module(module), fn)


def keys() -> list[str]:
    return [p["key"] for p in PARAMETERS]
