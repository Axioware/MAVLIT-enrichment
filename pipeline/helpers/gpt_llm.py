import json
import logging
import re

import httpx

from config import OPENAI_KEY

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openai.com/v1"
_MODEL = "gpt-5-mini"
# gpt-5-mini rejects any custom "temperature" value (only the model default
# is accepted) — confirmed live: passing temperature=0 returns a 400
# "Unsupported value" error. Do not add a temperature param to these calls.

# text-embedding-3-small supports a "dimensions" override, unlike Mistral's
# mistral-embed which is fixed at 1024. Requesting 1024 here matches the
# existing brand_match_profile.embedding / creator_profiles.embedding
# Vector(1024) columns exactly, so no schema migration is needed.
_EMBED_MODEL = "text-embedding-3-small"
_EMBED_DIMENSIONS = 1024

_TIMEOUT = 30.0


def _headers() -> dict:
    return {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}


def fill_template(template: str, **kwargs) -> str:
    """Replace {var} placeholders only. Leaves JSON examples like {"key": val} untouched."""
    return re.sub(r'\{(\w+)\}', lambda m: kwargs.get(m.group(1), m.group(0)), template)


def call_gpt_json(prompt: str, context: str = "") -> dict:
    """
    Send prompt to OpenAI (gpt-5-mini) and parse the JSON response.
    Returns {} on any failure so callers can apply their own fallback.
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping LLM call%s", f" ({context})" if context else "")
        return {}
    try:
        resp = httpx.post(
            f"{_BASE_URL}/chat/completions",
            headers=_headers(),
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        logger.error("OpenAI JSON call failed%s — %s", f" ({context})" if context else "", exc)
        return {}


def call_gpt_text(prompt: str, context: str = "") -> str:
    """
    Send prompt to OpenAI (gpt-5-mini) and return the raw text response.
    Returns "" on any failure.
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping LLM call%s", f" ({context})" if context else "")
        return ""
    try:
        resp = httpx.post(
            f"{_BASE_URL}/chat/completions",
            headers=_headers(),
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.error("OpenAI text call failed%s — %s", f" ({context})" if context else "", exc)
        return ""


def embed_text(text: str, context: str = "") -> list[float]:
    """
    Embed a string via OpenAI's text-embedding-3-small model, requested at
    1024 dims to match brand_match_profile.embedding / creator_profiles.embedding.
    Returns [] on any failure so callers can decide how to handle a missing
    embedding (e.g. skip the write rather than store a malformed vector).
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping embed call%s", f" ({context})" if context else "")
        return []
    try:
        resp = httpx.post(
            f"{_BASE_URL}/embeddings",
            headers=_headers(),
            json={"model": _EMBED_MODEL, "input": [text], "dimensions": _EMBED_DIMENSIONS},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as exc:
        logger.error("OpenAI embed call failed%s — %s", f" ({context})" if context else "", exc)
        return []
