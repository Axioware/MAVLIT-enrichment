import json
import logging
import re
import threading
import time

import httpx

from config import EMBEDDING_MODEL, OPENAI_KEY

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
_EMBED_MODEL = EMBEDDING_MODEL
_EMBED_DIMENSIONS = 1024

_TIMEOUT = 60.0

# ---------------------------------------------------------------------------
# OpenAI unavailable (credits / quota / rate limit / outage)
# ---------------------------------------------------------------------------
# Before, every failure came back as {} / "" / [] and callers treated that as
# "the model had nothing to say" — so an out-of-credits OpenAI account made
# batch steps mark every row as processed (shopify_checked, instagram_checked,
# brand_tier 'null', ...) with no data. Now an UNAVAILABLE failure (no credits
# or quota, auth error, rate limit, 5xx, network) is retried once; when a
# second consecutive call fails too, LLMUnavailableError is raised and the run
# stops BEFORE the row being worked on is marked as done. Requests that are
# simply invalid (HTTP 400/404/422) and unparseable model output still return
# {} / "" / [] as before.
_STOP_AFTER_FAILURES = 2      # consecutive unavailable errors that stop the run
_COOLDOWN_SECONDS = 60        # after stopping, calls fail fast for this long
_MAX_RETRY_AFTER = 30

_state_lock = threading.Lock()
_consecutive_failures = 0
_stopped_until = 0.0
_stopped_reason = ""


class LLMUnavailableError(RuntimeError):
    """
    OpenAI is unusable right now (credits/quota exhausted, bad key, rate
    limited, outage) — raised after two consecutive failed calls so the run
    stops WITHOUT marking the row being processed as done.

    Batch scripts and orchestrators that catch `Exception` per row/step to
    carry on must let this one through (`except LLMUnavailableError: raise`
    before the generic handler) — carrying on would only keep scraping rows
    that can never be finished. API job wrappers need nothing special: their
    generic handler records the failure on the job like any other error.
    """


def _error_body(exc: "httpx.HTTPStatusError") -> tuple[str, str]:
    """(error code/type lower-cased, message) from an OpenAI error response."""
    try:
        err = exc.response.json().get("error") or {}
    except Exception:
        return "", exc.response.text[:200] if exc.response is not None else ""
    code = f"{err.get('code') or ''} {err.get('type') or ''}".strip().lower()
    return code, str(err.get("message") or "")[:300]


def _classify_failure(exc: Exception) -> tuple[str | None, str, float]:
    """
    (kind, detail, retry_delay_seconds). kind is None for a request that is
    invalid on its own (400/404/422...) — not an availability problem.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code, message = _error_body(exc)
        haystack = f"{code} {message}".lower()
        quota = any(w in haystack for w in ("insufficient_quota", "quota", "billing", "credit", "hard_limit"))
        detail = f"HTTP {status} {code} — {message}".strip(" —")
        if status in (401, 403):
            return "bad key / no access", detail, 2.0
        if status == 402 or (status in (400, 429) and quota):
            return "credits or quota exhausted", detail, 2.0
        if status == 429:
            try:
                delay = min(float(exc.response.headers.get("retry-after", 5)), _MAX_RETRY_AFTER)
            except ValueError:
                delay = 5.0
            return "rate limited", detail, delay
        if status >= 500:
            return "OpenAI server error", detail, 3.0
        return None, detail, 0.0
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return "network / timeout", f"{type(exc).__name__}: {exc}", 3.0
    return None, str(exc), 0.0


def _post_openai(path: str, payload: dict, timeout: float, context: str = "") -> dict:
    """
    POST to OpenAI and return the decoded JSON body. Retries one unavailable
    failure once, raises LLMUnavailableError on the second consecutive one.
    Any other failure is re-raised as is, for the caller's own handler.
    """
    global _consecutive_failures, _stopped_until, _stopped_reason
    with _state_lock:
        if time.monotonic() < _stopped_until:
            raise LLMUnavailableError(_stopped_reason)
    label = f" ({context})" if context else ""
    while True:
        try:
            resp = httpx.post(f"{_BASE_URL}{path}", headers=_headers(), json=payload, timeout=timeout)
            resp.raise_for_status()
            with _state_lock:
                _consecutive_failures = 0
            return resp.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            kind, detail, delay = _classify_failure(exc)
            if kind is None:
                raise
            with _state_lock:
                _consecutive_failures += 1
                count = _consecutive_failures
                if count >= _STOP_AFTER_FAILURES:
                    _consecutive_failures = 0
                    _stopped_reason = f"OpenAI unavailable ({kind}): {detail}"
                    _stopped_until = time.monotonic() + _COOLDOWN_SECONDS
            logger.error("OpenAI call failed%s — %s: %s (failure %d/%d)",
                         label, kind, detail, count, _STOP_AFTER_FAILURES)
            if count >= _STOP_AFTER_FAILURES:
                logger.error(
                    "%s — %d consecutive failures, STOPPING THE RUN. The row being processed was not marked "
                    "as done; re-run once OpenAI is available again.", _stopped_reason, _STOP_AFTER_FAILURES,
                )
                raise LLMUnavailableError(_stopped_reason) from exc
            time.sleep(delay)


def _headers() -> dict:
    return {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}


def fill_template(template: str, **kwargs) -> str:
    """Replace {var} placeholders only. Leaves JSON examples like {"key": val} untouched."""
    return re.sub(r'\{(\w+)\}', lambda m: kwargs.get(m.group(1), m.group(0)), template)


def call_gpt_json(
    prompt: str,
    context: str = "",
    timeout: float = _TIMEOUT,
    model: str = _MODEL,
) -> dict:
    """
    Send a prompt to the selected OpenAI model and parse the JSON response.
    Returns {} when the request is invalid or the output is not parseable JSON
    so callers can apply their own fallback. Raises LLMUnavailableError (see
    above) when OpenAI itself is unavailable — never {} for that.

    timeout overrides the default 60s — pass a larger value for prompts
    with an unusually large payload (e.g. ranking up to 50 candidates in
    apollo_contacts.py, which can genuinely take longer than 60s and would
    otherwise silently fall back to non-LLM ranking every time it does).
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping LLM call%s", f" ({context})" if context else "")
        return {}
    try:
        body = _post_openai(
            "/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout, context,
        )
        raw = body["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except LLMUnavailableError:
        raise   # OpenAI is unavailable — never turn that into an empty result
    except Exception as exc:
        logger.error("OpenAI JSON call failed%s — %s", f" ({context})" if context else "", exc)
        return {}


def call_gpt_text(prompt: str, context: str = "") -> str:
    """
    Send prompt to OpenAI (gpt-5-mini) and return the raw text response.
    Returns "" on an invalid request / unparseable response; raises
    LLMUnavailableError when OpenAI itself is unavailable.
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping LLM call%s", f" ({context})" if context else "")
        return ""
    try:
        body = _post_openai(
            "/chat/completions",
            {"model": _MODEL, "messages": [{"role": "user", "content": prompt}]},
            _TIMEOUT, context,
        )
        return body["choices"][0]["message"]["content"].strip()
    except LLMUnavailableError:
        raise   # OpenAI is unavailable — never turn that into an empty result
    except Exception as exc:
        logger.error("OpenAI text call failed%s — %s", f" ({context})" if context else "", exc)
        return ""


def embed_text(text: str, context: str = "") -> list[float]:
    """
    Embed a string via OpenAI's text-embedding-3-small model, requested at
    1024 dims to match brand_match_profile.embedding / creator_profiles.embedding.
    Returns [] on an invalid request so callers can decide how to handle a
    missing embedding (e.g. skip the write rather than store a malformed
    vector); raises LLMUnavailableError when OpenAI itself is unavailable.
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping embed call%s", f" ({context})" if context else "")
        return []
    try:
        body = _post_openai(
            "/embeddings",
            {"model": _EMBED_MODEL, "input": [text], "dimensions": _EMBED_DIMENSIONS},
            _TIMEOUT, context,
        )
        return body["data"][0]["embedding"]
    except LLMUnavailableError:
        raise   # OpenAI is unavailable — never turn that into an empty result
    except Exception as exc:
        logger.error("OpenAI embed call failed%s — %s", f" ({context})" if context else "", exc)
        return []


def embed_text_batch(texts: list[str], context: str = "") -> list[list[float]]:
    """Embed multiple strings in one OpenAI request, returning [] on an invalid request / bad response."""
    if not texts:
        return []
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping embed call%s", f" ({context})" if context else "")
        return []
    try:
        body = _post_openai(
            "/embeddings",
            {"model": _EMBED_MODEL, "input": texts, "dimensions": _EMBED_DIMENSIONS},
            _TIMEOUT, context,
        )
        data = sorted(body["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in data]
        if len(vectors) != len(texts) or any(len(vector) != _EMBED_DIMENSIONS for vector in vectors):
            raise ValueError("OpenAI returned an unexpected number or dimension of embeddings")
        return vectors
    except LLMUnavailableError:
        raise   # OpenAI is unavailable — never turn that into an empty result
    except Exception as exc:
        logger.error("OpenAI batch embed call failed%s — %s", f" ({context})" if context else "", exc)
        return []
