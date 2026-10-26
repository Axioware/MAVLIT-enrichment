import contextlib
import contextvars
import json
import logging
import re
import threading
import time

import httpx

from config import EMBEDDING_MODEL, OPENAI_KEY

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openai.com/v1"
_MODEL = "gpt-6-luna"
# gpt-5-mini (and, to be safe, the newer models) reject any custom "temperature" value (only the model default
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
# LLM cost tracking
# ---------------------------------------------------------------------------
# Every successful _post_openai call is logged to the llm_usage table with its
# token usage and cost. USD per 1M tokens: (input, cached input, output).
# Reasoning tokens are billed as output (they are part of completion_tokens).
# Matched by longest model-name prefix, so dated snapshots such as
# "gpt-5-2025-08-07" use the "gpt-5" price. Update when OpenAI changes prices;
# a model missing here is still logged, with cost_usd NULL.
_PRICES = {
    "gpt-5":                  (1.25, 0.125, 10.00),
    "gpt-5-mini":             (0.25, 0.025, 2.00),
    # The two Luna prices are third-party reports of OpenAI's list prices (not read from OpenAI's page): verify.
    "gpt-5.6-luna":           (0.20, 0.02, 1.20),
    "gpt-6-luna":             (0.10, 0.01, 0.50),
    "gpt-5-nano":             (0.05, 0.005, 0.40),
    "gpt-5.4-mini":           (0.75, 0.075, 4.50),
    "text-embedding-3-small": (0.02, 0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.13, 0.0),
}


def _price_for(model: str) -> tuple[float, float, float] | None:
    matches = [name for name in _PRICES if model == name or model.startswith(name + "-")]
    return _PRICES[max(matches, key=len)] if matches else None


def llm_cost_usd(model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float | None:
    """Cost of one call in USD from its token usage, or None if the model has no price in _PRICES."""
    price = _price_for(model)
    if price is None:
        return None
    input_price, cached_price, output_price = price
    return (
        (prompt_tokens - cached_tokens) * input_price
        + cached_tokens * cached_price
        + completion_tokens * output_price
    ) / 1_000_000


# Who triggered the call. A MAVLIT-user action (generate my description, profile
# signals, matches v2/v3, pitch, ...) wraps its work in llm_usage_scope(); every
# call logged inside it carries that creator_profile_id + feature, so the cost
# can be totalled per user. Calls made outside a scope (the batch pipeline)
# stay NULL. Worker threads do not inherit it - wrap their function with scoped().
_usage_scope: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar("llm_usage_scope", default=None)


@contextlib.contextmanager
def llm_usage_scope(creator_profile_id: int, feature: str):
    """Attribute every LLM call made inside this block to a MAVLIT user and feature."""
    token = _usage_scope.set((creator_profile_id, feature))
    try:
        yield
    finally:
        _usage_scope.reset(token)


# A caller that needs the tokens, cost and raw answer of the calls it makes (the rate-intelligence log) wraps them in
# capture_llm_usage(); every call made in this thread inside the block appends one dict to the yielded list.
_usage_capture: contextvars.ContextVar[list | None] = contextvars.ContextVar("llm_usage_capture", default=None)


@contextlib.contextmanager
def capture_llm_usage():
    calls: list[dict] = []
    token = _usage_capture.set(calls)
    try:
        yield calls
    finally:
        _usage_capture.reset(token)


def scoped(fn):
    """
    Wrap fn so that, when it runs in a ThreadPoolExecutor worker, its LLM calls
    are attributed like the scope active HERE (call scoped() in the submitting
    thread, e.g. `pool.map(scoped(work), items)`).
    """
    scope = _usage_scope.get()

    def run(*args, **kwargs):
        token = _usage_scope.set(scope)
        try:
            return fn(*args, **kwargs)
        finally:
            _usage_scope.reset(token)

    return run


def record_apify_scrape(platform: str, handle: str, posts_requested: int, posts_scraped: int) -> None:
    """
    Log how many posts an Apify scrape returned for the MAVLIT user of the current
    llm_usage_scope (no-op outside one). No cost is recorded. Never raises.
    """
    scope = _usage_scope.get()
    if scope is None:
        return
    try:
        from pipeline.db import ApifyUsage, SessionLocal, engine   # lazy, as in _record_usage

        ApifyUsage.__table__.create(bind=engine, checkfirst=True)
        with SessionLocal() as db:
            db.add(ApifyUsage(
                creator_profile_id=scope[0], feature=scope[1], platform=platform, handle=handle,
                posts_requested=posts_requested, posts_scraped=posts_scraped,
            ))
            db.commit()
    except Exception as exc:
        logger.warning("Apify usage: could not record scrape for %s — %s", handle, exc)


_usage_table_ready = False


def _record_usage(path: str, payload: dict, body: dict, context: str, seconds: float) -> None:
    """Log one successful call to llm_usage. Never raises — tracking must not break an LLM call."""
    global _usage_table_ready
    try:
        from pipeline.db import LlmUsage, SessionLocal, engine   # lazy: pipeline.db must not load at import time

        if not _usage_table_ready:
            # Scripts can run before the API's create_all has made the table.
            LlmUsage.__table__.create(bind=engine, checkfirst=True)
            _usage_table_ready = True

        usage = body.get("usage") or {}
        model = str(payload.get("model") or body.get("model") or "unknown")
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        cost = llm_cost_usd(model, prompt_tokens, cached_tokens, completion_tokens)
        if cost is None:
            logger.warning("LLM cost: no price for model %r in gpt_llm._PRICES — logged without cost", model)
        captured = _usage_capture.get()
        if captured is not None:
            try:
                answer = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                answer = None
            captured.append({
                "model": model, "prompt_tokens": prompt_tokens, "cached_tokens": cached_tokens,
                "completion_tokens": completion_tokens, "reasoning_tokens": reasoning_tokens,
                "cost_usd": cost, "duration_ms": round(seconds * 1000), "output": answer,
            })
        scope = _usage_scope.get()
        db = SessionLocal()
        try:
            db.add(LlmUsage(
                creator_profile_id=scope[0] if scope else None,
                feature=scope[1] if scope else None,
                model=model,
                endpoint=path,
                context=context or None,
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
                cost_usd=cost,
                duration_ms=round(seconds * 1000),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("LLM cost: could not record usage%s — %s", f" ({context})" if context else "", exc)

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
            started = time.monotonic()
            resp = httpx.post(f"{_BASE_URL}{path}", headers=_headers(), json=payload, timeout=timeout)
            resp.raise_for_status()
            with _state_lock:
                _consecutive_failures = 0
            body = resp.json()
            _record_usage(path, payload, body, context, time.monotonic() - started)
            return body
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
    Send prompt to OpenAI (the default _MODEL) and return the raw text response.
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


def stream_gpt_text(prompt: str, context: str = "", scope: tuple[int, str] | None = None):
    """
    Like call_gpt_text, but yields the answer in pieces as OpenAI writes it (chat completions
    with stream=true), so a caller can show it live. The call is logged to llm_usage when the
    stream ends, with the real token usage. Raises LLMUnavailableError when OpenAI is unavailable
    (credits, key, rate limit, outage, network) and RuntimeError for any other failure; text already
    yielded before an error cannot be taken back, so callers should treat the whole as failed.
    scope=(creator_profile_id, feature) attributes the logged cost to a MAVLIT user (passed in rather
    than read from llm_usage_scope: a generator can resume in a different context).
    """
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_KEY not set")
    with _state_lock:
        if time.monotonic() < _stopped_until:
            raise LLMUnavailableError(_stopped_reason)
    path = "/chat/completions"
    payload = {
        "model": _MODEL, "messages": [{"role": "user", "content": prompt}],
        "stream": True, "stream_options": {"include_usage": True},
    }
    started = time.monotonic()
    usage: dict = {}
    try:
        with httpx.stream("POST", f"{_BASE_URL}{path}", headers=_headers(), json=payload, timeout=_TIMEOUT) as resp:
            if resp.status_code >= 400:
                resp.read()
                resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        kind, detail, _ = _classify_failure(exc)
        if kind is not None:
            raise LLMUnavailableError(f"OpenAI unavailable ({kind}): {detail}") from exc
        raise RuntimeError(f"OpenAI request failed: {detail}") from exc
    if scope is not None:
        with llm_usage_scope(*scope):
            _record_usage(path, payload, {"usage": usage, "model": _MODEL}, context, time.monotonic() - started)
    else:
        _record_usage(path, payload, {"usage": usage, "model": _MODEL}, context, time.monotonic() - started)


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
