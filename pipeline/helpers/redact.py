"""
pipeline/helpers/redact.py

Keep API keys out of logs. Several providers take the key as a URL query parameter (YouTube `key=`, Hunter `api_key=`, Meta `access_token=`), and
an httpx error such as the one `raise_for_status()` raises contains the whole request URL in its message. Whatever logs an exception text
passes it through redact(); describe_http_error() gives the safe one-line summary (the status code) for an HTTP failure; and
install_redaction(logger) puts a filter on a logger so that its messages AND the tracebacks it prints are scrubbed whatever the call site does.
"""

import logging
import re

REDACTED = "[redacted]"

# ?key=...&access_token=...&api_key=...: the value goes, the parameter name stays (so the log still shows which parameter it was)
_QUERY_SECRET = re.compile(r"(?i)(?<![A-Za-z0-9])(key|api_key|apikey|access_token|token|client_secret)=([^&\s\"'<>#]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}")
_KEY_SHAPES = re.compile(r"\b(?:sk-[A-Za-z0-9_\-*.]{4,}|AIza[0-9A-Za-z_\-]{20,}|apify_api_[A-Za-z0-9]{6,})")


def redact(value) -> str:
    """The text with key=, api_key=, access_token= (and token=, apikey=, client_secret=) query values, Bearer tokens and key-shaped strings removed."""
    text = value if isinstance(value, str) else str(value)
    text = _QUERY_SECRET.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    return _KEY_SHAPES.sub(REDACTED, text)


def describe_http_error(exc: BaseException) -> str:
    """A log-safe description of a failed HTTP call: the status code for a bad response, otherwise only the kind of error. Never the URL."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


class RedactingFilter(logging.Filter):
    """Scrubs the message, its arguments and the traceback of every record the logger it is attached to handles."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:   # never let the scrubber break logging
            pass
        return True


def install_redaction(logger: logging.Logger) -> None:
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
