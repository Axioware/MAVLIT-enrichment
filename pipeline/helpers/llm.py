"""
pipeline/helpers/llm.py

Shared Mistral LLM helpers used across enrichment steps.
Requires MISTRAL_API_KEY in config / .env.
"""

import json
import logging
import re

from config import MISTRAL_API_KEY

logger = logging.getLogger(__name__)

_MODEL = "mistral-small-latest"


def _get_client():
    from mistralai.client.sdk import Mistral
    return Mistral(api_key=MISTRAL_API_KEY)


def fill_template(template: str, **kwargs) -> str:
    """Replace {var} placeholders only. Leaves JSON examples like {"key": val} untouched."""
    return re.sub(r'\{(\w+)\}', lambda m: kwargs.get(m.group(1), m.group(0)), template)


def call_mistral_json(prompt: str, context: str = "") -> dict:
    """
    Send prompt to Mistral and parse the JSON response.
    Returns {} on any failure so callers can apply their own fallback.
    """
    if not MISTRAL_API_KEY:
        logger.warning("MISTRAL_API_KEY not set — skipping LLM call%s", f" ({context})" if context else "")
        return {}
    try:
        client = _get_client()
        resp = client.chat.complete(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        logger.error("Mistral JSON call failed%s — %s", f" ({context})" if context else "", exc)
        return {}


def call_mistral_text(prompt: str, context: str = "") -> str:
    """
    Send prompt to Mistral and return the raw text response.
    Returns "" on any failure.
    """
    if not MISTRAL_API_KEY:
        logger.warning("MISTRAL_API_KEY not set — skipping LLM call%s", f" ({context})" if context else "")
        return ""
    try:
        client = _get_client()
        resp = client.chat.complete(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("Mistral text call failed%s — %s", f" ({context})" if context else "", exc)
        return ""
