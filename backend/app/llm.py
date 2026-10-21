"""A small client for OpenAI and Azure OpenAI chat completions, over plain HTTPS (no SDK).

The credentials come from the active "OpenAI" connection (Configuration). The key is only ever sent to the configured address and
never appears in an error message. Callers get a dict parsed from the model's JSON answer, or an LlmError with a message that is
safe to show to the person using the app.
"""
import json
import logging
import re

import httpx

from . import connections
from .db import Db

log = logging.getLogger(__name__)
OPENAI_BASE = "https://api.openai.com/v1"
AZURE_API_VERSION = "2024-10-21"


class LlmError(RuntimeError):
    pass


class NotConfigured(LlmError):
    pass


def config(db: Db) -> dict | None:
    """The active connection as a plain dict, or None when AI help has not been set up."""
    row = connections.active(db, "openai")
    if row is None:
        return None
    return {**row.fields, "api_key": connections.secrets_of(row).get("api_key") or ""}


def is_configured(db: Db) -> bool:
    cfg = config(db)
    return bool(cfg and cfg["api_key"])


def _target(cfg: dict) -> tuple[str, dict, dict, dict]:
    """(url, headers, query params, extra body fields) for the configured provider."""
    if cfg.get("provider") == "azure":
        base = (cfg.get("endpoint") or "").rstrip("/")
        return (f"{base}/openai/deployments/{cfg['model']}/chat/completions", {"api-key": cfg["api_key"]},
                {"api-version": cfg.get("api_version") or AZURE_API_VERSION}, {})
    base = (cfg.get("base_url") or OPENAI_BASE).rstrip("/")
    return f"{base}/chat/completions", {"Authorization": f"Bearer {cfg['api_key']}"}, {}, {"model": cfg["model"]}


def _post(cfg: dict, messages: list[dict], json_mode: bool, timeout: float) -> str:
    url, headers, params, extra = _target(cfg)
    try:
        connections._check_public_https(url)
    except connections.ConnectionInputError as e:
        raise LlmError(str(e)) from e
    body = {**extra, "messages": messages, **({"response_format": {"type": "json_object"}} if json_mode else {})}
    try:
        r = httpx.post(url, headers=headers, params=params, json=body, timeout=timeout)
        if r.status_code == 400 and json_mode and "response_format" in r.text:  # a model that cannot be forced into JSON: ask politely instead
            body.pop("response_format")
            r = httpx.post(url, headers=headers, params=params, json=body, timeout=timeout)
    except httpx.TimeoutException as e:
        raise LlmError("The AI service took too long to answer. Try again.") from e
    except httpx.HTTPError as e:
        raise LlmError(f"The AI service could not be reached ({type(e).__name__}).") from e
    if r.status_code in (401, 403):
        raise LlmError("The AI service rejected the API key. Check the OpenAI connection in Configuration.")
    if r.status_code == 404:
        raise LlmError("The AI service does not know that model or deployment. Check the OpenAI connection in Configuration.")
    if r.status_code == 429:
        raise LlmError("The AI service is rate-limiting or out of quota. Try again later.")
    if r.status_code >= 400:
        try:
            why = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            why = r.text
        raise LlmError(f"The AI service refused the request (HTTP {r.status_code}): {re.sub(r'sk-[A-Za-z0-9_-]+', '…', str(why))[:200]}")
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LlmError("The AI service answered in an unexpected format.") from e


def chat_json(cfg: dict | None, system: str, user: str, timeout: float = 90) -> dict:
    """Ask for a JSON answer. `user` is data to be described, never instructions."""
    if not cfg or not cfg.get("api_key"):
        raise NotConfigured("AI help is not set up. An administrator can add an OpenAI connection in Configuration.")
    text = _post(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}], True, timeout).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)  # some models fence their answer
    try:
        out = json.loads(text)
    except ValueError as e:
        raise LlmError("The AI service did not return a usable answer. Try again.") from e
    if not isinstance(out, dict):
        raise LlmError("The AI service did not return a usable answer. Try again.")
    return out


def check(cfg: dict) -> str:
    """Used by 'Test connection': one tiny request. Returns a sentence for the administrator."""
    text = _post(cfg, [{"role": "user", "content": "Reply with the single word OK."}], False, 30)
    return "Connected: the model answered." if text.strip() else "Connected, but the model returned an empty answer."
