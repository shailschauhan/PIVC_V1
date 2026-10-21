from dataclasses import dataclass, field
from typing import Any


@dataclass
class Context:
    """Everything an analyzer needs for one case."""
    case_ref: str
    media: dict[str, Any]
    rules: dict[str, Any]
    declared: dict[str, Any] = field(default_factory=dict)  # optional details supplied with the upload (declared age / DOB)
    obs: dict[str, Any] = field(default_factory=dict)  # model observations, filled by the provider


def result(status: str, confidence: float, summary: str, details: dict | None = None,
           exceptions: list[str] | None = None) -> dict:
    return {
        "status": status,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "summary": summary,
        "details": details or {},
        "exceptions": exceptions or [],
    }
