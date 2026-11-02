"""
API keys must never reach a log. YouTube, Hunter and Meta take their key as a URL query parameter, and an httpx error carries the whole URL in its
message; OpenAI error texts carry a fragment of a bad key. These tests make real calls fail with a FAKE key in the URL and read back everything
the loggers wrote: messages and tracebacks.
"""

import logging
import re
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from pipeline.helpers import gpt_llm
from pipeline.helpers.redact import REDACTED, RedactingFilter, describe_http_error, install_redaction, redact

ROOT = Path(__file__).resolve().parent.parent
FAKE = "FAKEKEY0123456789secret"


def response(status, url, *, json=None, text=None):
    request = httpx.Request("GET", url)
    return httpx.Response(status, json=json, text=text, request=request) if text is None else httpx.Response(status, text=text, request=request)


class Captured:
    """Everything a logger writes, formatted the way a handler would (message and traceback)."""

    def __init__(self, logger):
        self.records, self.logger = [], logger
        self.handler = logging.Handler(logging.DEBUG)
        self.handler.emit = lambda record: self.records.append(self.handler.format(record))
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))

    def __enter__(self):
        self.old_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    @property
    def text(self):
        return "\n".join(self.records)


class RedactHelper(unittest.TestCase):
    def test_query_parameters_lose_their_values(self):
        url = f"https://www.googleapis.com/youtube/v3/search?q=music&key={FAKE}&maxResults=5"
        self.assertEqual(redact(url), f"https://www.googleapis.com/youtube/v3/search?q=music&key={REDACTED}&maxResults=5")
        for name in ("key", "api_key", "access_token", "token", "apikey", "KEY", "Access_Token"):
            out = redact(f"GET https://x.example/path?{name}={FAKE}&email=a%40b.com failed")
            self.assertNotIn(FAKE, out, name)
            self.assertIn("email=a%40b.com", out, "other parameters are kept")
            self.assertIn(f"{name}={REDACTED}", out)

    def test_the_first_and_only_parameter_and_quoted_urls(self):
        self.assertNotIn(FAKE, redact(f"https://h.example/v2/email-verifier?api_key={FAKE}"))
        self.assertNotIn(FAKE, redact(f"Client error '401 Unauthorized' for url 'https://h.example/x?email=a&api_key={FAKE}'\nFor more information..."))
        self.assertNotIn(FAKE, redact(f'{{"url": "https://h.example/?access_token={FAKE}"}}'))

    def test_bearer_tokens_and_key_shapes(self):
        self.assertNotIn(FAKE, redact(f"Authorization: Bearer {FAKE}"))
        self.assertNotIn("abc123", redact("Incorrect API key provided: sk-proj-abc123***xyz9."))
        self.assertNotIn("AIzaSyA1234567890abcdefghijklmnopqrstu", redact("key AIzaSyA1234567890abcdefghijklmnopqrstu was rejected"))
        self.assertNotIn("apify_api_ABCdef123456", redact("token apify_api_ABCdef123456 expired"))

    def test_ordinary_text_is_unchanged_and_the_helper_is_safe(self):
        for text in ("Meta Ads API 400 for 'Nike'", "monkey=1 and keyboard=2", "status=200", "HTTP 429 insufficient_quota — You exceeded your current quota", ""):
            self.assertEqual(redact(text), text)
        self.assertEqual(redact(None), "None")
        self.assertEqual(redact(ValueError(f"bad ?key={FAKE}")), f"bad ?key={REDACTED}")
        once = redact(f"?key={FAKE}")
        self.assertEqual(redact(once), once)

    def test_describe_http_error_gives_only_the_status_or_the_kind(self):
        bad = httpx.HTTPStatusError("Client error for url 'https://x?key=" + FAKE + "'", request=httpx.Request("GET", "https://x"),
                                    response=httpx.Response(403, request=httpx.Request("GET", "https://x")))
        self.assertEqual(describe_http_error(bad), "HTTP 403")
        self.assertEqual(describe_http_error(httpx.ConnectError(f"cannot connect ?key={FAKE}")), "ConnectError")
        self.assertEqual(describe_http_error(ValueError("x")), "ValueError")


class FilterOnALogger(unittest.TestCase):
    def test_messages_arguments_and_tracebacks_are_scrubbed(self):
        logger = logging.getLogger("test.redaction.filter")
        install_redaction(logger)
        install_redaction(logger)
        self.assertEqual(len([f for f in logger.filters if isinstance(f, RedactingFilter)]), 1, "installed once")
        with Captured(logger) as log:
            logger.warning("call failed: %s (see %s)", f"https://x?access_token={FAKE}", "https://x?q=1")
            try:
                raise RuntimeError(f"upstream said https://x/?api_key={FAKE}")
            except RuntimeError:
                logger.exception("it broke")
        self.assertNotIn(FAKE, log.text)
        self.assertIn("https://x?q=1", log.text, "everything else is kept")
        self.assertIn("RuntimeError", log.text, "the traceback is still there")
        self.assertIn("it broke", log.text)

    def test_a_record_with_no_secret_is_untouched(self):
        logger = logging.getLogger("test.redaction.plain")
        install_redaction(logger)
        with Captured(logger) as log:
            logger.error("100%% sure for %s", "Nike")
        self.assertIn("100% sure for Nike", log.text)


class YouTube(unittest.TestCase):
    URL = f"https://www.googleapis.com/youtube/v3/search?q=music&key={FAKE}"

    def setUp(self):
        from pipeline.enrichment import youtube_sponsorship as yt
        self.yt = yt
        for patcher in (mock.patch.object(yt, "_active_key", return_value=FAKE), mock.patch.object(yt.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_bad_response_logs_the_status_and_nothing_of_the_url(self):
        bad = response(400, self.URL, json={"error": {"message": "Bad request"}})
        with mock.patch.object(self.yt.httpx, "get", return_value=bad), Captured(self.yt.logger) as log:
            self.assertIsNone(self.yt._yt_get(self.yt._SEARCH_URL, {"q": "music"}))
        self.assertNotIn(FAKE, log.text)
        self.assertIn("YouTube API call failed (HTTP 400)", log.text)

    def test_a_connection_failure_with_the_url_in_its_message(self):
        error = httpx.ConnectError(f"cannot reach {self.URL}")
        with mock.patch.object(self.yt.httpx, "get", side_effect=error), Captured(self.yt.logger) as log:
            self.assertIsNone(self.yt._yt_get(self.yt._SEARCH_URL, {"q": "music"}))
        self.assertNotIn(FAKE, log.text)
        self.assertIn("YouTube API call failed (ConnectError)", log.text)

    def test_a_rejected_key_message_from_google_is_scrubbed_too(self):
        dead = response(400, self.URL, json={"error": {"message": f"API key not valid. key={FAKE}",
                                                       "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}})
        with mock.patch.object(self.yt.httpx, "get", return_value=dead), mock.patch.object(self.yt, "_rotate_key", side_effect=self.yt._QuotaExhausted()), \
                Captured(self.yt.logger) as log:
            with self.assertRaises(self.yt._QuotaExhausted):
                self.yt._yt_get(self.yt._SEARCH_URL, {"q": "music"})
        self.assertNotIn(FAKE, log.text)


class Hunter(unittest.TestCase):
    URL = f"https://api.hunter.io/v2/email-verifier?email=a%40b.com&api_key={FAKE}"

    def setUp(self):
        from pipeline.enrichment import hunter_verify as hv
        self.hv = hv

    def test_a_failed_status_logs_the_code_and_a_scrubbed_body(self):
        bad = response(401, self.URL, text=f'{{"errors":[{{"details":"Invalid api_key={FAKE}"}}]}}')
        with mock.patch.object(self.hv.httpx, "get", return_value=bad), Captured(self.hv.logger) as log:
            self.assertIsNone(self.hv._verify_email("a@b.com"))
        self.assertNotIn(FAKE, log.text)
        self.assertIn("HTTP 401", log.text)

    def test_a_request_error_with_the_url_in_its_message(self):
        error = httpx.UnsupportedProtocol(f"Request URL has an unsupported protocol: {self.URL}")
        with mock.patch.object(self.hv.httpx, "get", side_effect=error), Captured(self.hv.logger) as log:
            self.assertIsNone(self.hv._verify_email("a@b.com"))
        self.assertNotIn(FAKE, log.text)
        self.assertIn("UnsupportedProtocol", log.text)


class Meta(unittest.TestCase):
    def setUp(self):
        from pipeline.enrichment import meta_ads
        self.meta = meta_ads
        patcher = mock.patch.object(meta_ads, "META_ACCESS_TOKEN", FAKE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_call_that_fails_with_the_token_in_the_error_text_and_traceback(self):
        error = httpx.UnsupportedProtocol(f"bad url https://graph.facebook.com/v21.0/ads_archive?access_token={FAKE}")
        with mock.patch.object(self.meta.httpx, "get", side_effect=error), Captured(self.meta.logger) as log:
            with self.assertRaises(self.meta._FetchError):
                self.meta._fetch_ads(page_id=None, brand_name="Nike")
        self.assertNotIn(FAKE, log.text)
        self.assertIn("Meta Ads API call failed for 'Nike'", log.text)
        self.assertIn("UnsupportedProtocol", log.text, "the traceback is still logged")

    def test_an_error_body_that_repeats_the_token(self):
        body = {"error": {"code": 100, "message": f"Unsupported request, access_token={FAKE}"}}
        bad = response(400, "https://graph.facebook.com/v21.0/ads_archive", json=body)
        with mock.patch.object(self.meta.httpx, "get", return_value=bad), Captured(self.meta.logger) as log:
            with self.assertRaises(self.meta._FetchError):
                self.meta._fetch_ads(page_id="123", brand_name="Nike")
        self.assertNotIn(FAKE, log.text)

    def test_the_page_id_lookup_error_path(self):
        error = httpx.UnsupportedProtocol(f"https://graph.facebook.com/v21.0/nike?fields=id&access_token={FAKE}")
        with mock.patch.object(self.meta.httpx, "get", side_effect=error), Captured(self.meta.logger) as log:
            self.assertIsNone(self.meta._resolve_page_id("nike"))
        self.assertNotIn(FAKE, log.text)


class OpenAi(unittest.TestCase):
    def test_a_bad_key_error_text_keeps_the_key_fragment_out_of_the_log(self):
        fragment = "sk-proj-abc123"
        message = f"Incorrect API key provided: {fragment}***wxyz. You can find your API key at https://platform.openai.com/account/api-keys."
        bad = httpx.Response(401, json={"error": {"message": message, "code": "invalid_api_key"}}, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
        gpt_llm._consecutive_failures, gpt_llm._stopped_until = 0, 0.0
        self.addCleanup(lambda: (setattr(gpt_llm, "_consecutive_failures", 0), setattr(gpt_llm, "_stopped_until", 0.0)))
        with mock.patch.object(gpt_llm.httpx, "post", return_value=bad), mock.patch.object(gpt_llm.time, "sleep"), \
                mock.patch.object(gpt_llm, "OPENAI_KEY", "test-key"), mock.patch.object(gpt_llm, "_record_usage"), Captured(gpt_llm.logger) as log:
            with self.assertRaises(gpt_llm.LLMUnavailableError):
                gpt_llm._post_openai("/chat/completions", {}, 5.0, "test")
        self.assertNotIn(fragment, log.text)
        self.assertIn("bad key / no access", log.text, "the useful part of the message stays")


class SourceChecks(unittest.TestCase):
    FILES = ("pipeline/enrichment/youtube_sponsorship.py", "pipeline/enrichment/hunter_verify.py", "pipeline/enrichment/meta_ads.py")

    def test_these_modules_never_log_a_raw_exception(self):
        for name in self.FILES:
            source = (ROOT / name).read_text()
            self.assertIn("install_redaction(logger)", source, name)
            for number, line in enumerate(source.splitlines(), 1):
                if re.search(r"logger\.\w+\(", line) and re.search(r"[\"'],\s*(exc|e|err|error)\s*\)", line):
                    self.fail(f"{name}:{number} logs a raw exception: {line.strip()}")

    def test_the_key_modules_install_the_filter(self):
        self.assertIn("install_redaction(logger)", (ROOT / "pipeline/helpers/gpt_llm.py").read_text())


class EnvExample(unittest.TestCase):
    def setUp(self):
        self.values = {}
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                self.values[key.strip()] = value.strip()

    def test_the_database_url_is_a_real_looking_placeholder(self):
        self.assertEqual(self.values["DATABASE_URL"], "postgresql+psycopg2://USER:PASSWORD@localhost:5432/DBNAME")

    def test_every_secret_variable_is_listed_and_empty(self):
        wanted = (["APOLLO_API_KEY", "META_ACCESS_TOKEN", "YOUTUBE_API_KEY", "APIFY_TOKEN", "HUNTER_API_KEY", "FIRECRAWL_API_KEY", "JINA_API_KEY",
                   "FACEBOOK_PROXIES", "OPENAI_KEY", "POSTHOG_PROJECT_TOKEN", "JWT_SECRET", "ADMIN_SESSION_SECRET", "ADMIN_PASSKEY"]
                  + [f"YOUTUBE_API_KEY_{n}" for n in range(1, 13)])
        for name in wanted:
            self.assertIn(name, self.values, name)
            self.assertEqual(self.values[name], "", f"{name} must be empty in the example")

    def test_every_secret_the_config_reads_is_documented(self):
        config = (ROOT / "config.py").read_text()
        read = set(re.findall(r"os\.getenv\(\"([A-Z0-9_]+)\"", config))
        secretish = {n for n in read if re.search(r"(KEY|TOKEN|SECRET|PASSKEY|PROXIES)", n) and n not in ("JINAAI_API_KEY", "ACCESS_TOKEN_DAYS", "MIN_SECRET_LENGTH")}
        commented = set(re.findall(r"^#\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), flags=re.MULTILINE))   # optional ones are shown as "# NAME="
        missing = secretish - set(self.values) - commented
        self.assertFalse(missing, f"missing from .env.example: {sorted(missing)}")

    def test_an_unused_variable_is_gone(self):
        self.assertNotIn("POSTHOG_PROJECT_ID", self.values)
        self.assertNotIn("POSTHOG_PROJECT_ID", (ROOT / "config.py").read_text())


if __name__ == "__main__":
    unittest.main()
