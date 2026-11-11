"""Offline regressions for quote reading without optional native SDK imports."""
from __future__ import annotations

import builtins
import io
import json
import os
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from lib import claude_http, ocr


KEY = "offline-key-not-a-secret"
ANSWER = {"model": "served-model", "stop_reason": "end_turn", "content": [
    {"type": "text", "text": '{"quote":"HELLO WORLD","confidence":"high"}'},
]}


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode("utf-8"))
        self.status = 200
        self.headers = Message()

    def getcode(self):
        return self.status


def http_error(status, message="offline API error", headers=None):
    hdrs = Message()
    for name, value in (headers or {}).items():
        hdrs[name] = value
    body = json.dumps({"type": "error", "error": {
        "type": "api_error", "message": message,
    }}).encode("utf-8")
    return HTTPError(claude_http.MESSAGES_URL, status, "offline", hdrs, io.BytesIO(body))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        opener_patch = patch("urllib.request.build_opener", return_value=self.opener)
        self.build_opener = opener_patch.start()
        self.addCleanup(opener_patch.stop)
        sleep_patch = patch("time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def send(self, **kwargs):
        return claude_http.create_message({"model": "offline-model", "max_tokens": 32,
            "messages": [{"role": "user", "content": "offline request"}]}, KEY, 12, **kwargs)

    def test_serializes_official_https_request_and_beta_header(self):
        self.opener.open.return_value = Response(ANSWER)
        result = self.send(beta="server-side-fallback-2026-07-01")
        self.assertEqual(result, ANSWER)
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_method(), "POST")
        headers = {name.lower(): value for name, value in req.header_items()}
        self.assertEqual(headers["authorization"], "Bearer " + KEY)
        self.assertEqual(headers["anthropic-version"], "2026-10-21")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["anthropic-beta"], "server-side-fallback-2026-07-01")
        self.assertEqual(json.loads(req.data)["model"], "offline-model")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 12)
        self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_retries_transient_statuses_and_network_failures(self):
        for first in (http_error(408), http_error(409), http_error(429),
                      http_error(500), http_error(529), URLError("offline network"),
                      TimeoutError("offline timeout")):
            with self.subTest(error=type(first).__name__, status=getattr(first, "code", None)):
                self.opener.open.reset_mock()
                self.sleep.reset_mock()
                self.opener.open.side_effect = [first, Response(ANSWER)]
                self.assertEqual(self.send(), ANSWER)
                self.assertEqual(self.opener.open.call_count, 2)
                self.sleep.assert_called_once()

    def test_retry_budget_is_three_attempts(self):
        self.opener.open.side_effect = [http_error(503), http_error(503), http_error(503)]
        with self.assertRaises(claude_http.APIError) as caught:
            self.send()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_network_retry_budget_is_three_attempts(self):
        self.opener.open.side_effect = URLError("offline network")
        with self.assertRaises(claude_http.TransportError):
            self.send()
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_permanent_statuses_are_not_retried(self):
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.sleep.reset_mock()
                self.opener.open.side_effect = http_error(status)
                with self.assertRaises(claude_http.APIError) as caught:
                    self.send()
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(self.opener.open.call_count, 1)
                self.sleep.assert_not_called()

    def test_explicit_retry_header_is_respected(self):
        self.opener.open.side_effect = http_error(503, headers={"x-should-retry": "false"})
        with self.assertRaises(claude_http.APIError):
            self.send()
        self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()
        self.opener.open.reset_mock()
        self.opener.open.side_effect = [http_error(400, headers={"x-should-retry": "true"}),
                                      Response(ANSWER)]
        self.assertEqual(self.send(), ANSWER)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_retry_after_seconds_milliseconds_and_date_are_bounded(self):
        for headers in ({"retry-after": "7"}, {"retry-after-ms": "7000"},
                        {"retry-after": "Wed, 01 Jan 2100 00:00:00 GMT"}):
            with self.subTest(headers=headers):
                self.sleep.reset_mock()
                self.opener.open.side_effect = [http_error(429, headers=headers), Response(ANSWER)]
                self.assertEqual(self.send(), ANSWER)
                self.sleep.assert_called_once()
                delay = self.sleep.call_args.args[0]
                self.assertGreater(delay, 0)
                self.assertLessEqual(delay, 60)
                if "GMT" not in next(iter(headers.values())):
                    self.assertEqual(delay, 7)

    def test_invalid_json_non_object_and_oversized_responses_are_rejected(self):
        for response in (io.BytesIO(b"not JSON"), Response(None), Response([]),
                         Response({"type": "error", "error": {"message": "offline"}}),
                         io.BytesIO(b"x" * (claude_http.MAX_RESPONSE_BYTES + 1))):
            with self.subTest(response=response):
                self.opener.open.reset_mock()
                self.opener.open.return_value = response
                with self.assertRaises(claude_http.TransportError):
                    self.send()
                self.assertEqual(self.opener.open.call_count, 1)

    def test_redirects_do_not_forward_credentials_or_retry(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.sleep.reset_mock()
                self.opener.open.side_effect = http_error(status, headers={
                    "Location": "https://other.invalid/capture", "x-should-retry": "true"})
                with self.assertRaises(claude_http.APIError):
                    self.send()
                self.assertEqual(self.opener.open.call_count, 1)
                self.sleep.assert_not_called()
                handler = self.build_opener.call_args.args[0]
                req = Request(claude_http.MESSAGES_URL, data=b"{}", headers={
                    "Authorization": "Bearer " + KEY})
                self.assertIsNone(handler.redirect_request(req, None, status, "redirect", {},
                                                         "https://other.invalid/capture"))

    def test_api_key_is_removed_from_api_errors(self):
        self.opener.open.side_effect = http_error(400, "invalid credential " + KEY)
        with self.assertRaises(claude_http.APIError) as caught:
            self.send()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, caught.exception.message)


class QuoteTests(unittest.TestCase):
    def setUp(self):
        key_patch = patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY})
        key_patch.start()
        self.addCleanup(key_patch.stop)

    def ask(self, hard=False):
        return ocr.ask("requested-model", "image/jpeg", "offline-base64", "Read this", hard)

    def test_reads_quote_with_sdk_native_imports_blocked(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"anthropic", "jiter", "pydantic"}:
                raise ImportError("An Application Control policy has blocked this file")
            return original_import(name, *args, **kwargs)

        opener = Mock()
        opener.open.return_value = Response(ANSWER)
        with patch("builtins.__import__", side_effect=guarded_import), \
             patch("urllib.request.build_opener", return_value=opener):
            result = self.ask()
        self.assertEqual(result.quote, "HELLO WORLD")
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.model, "served-model")
        self.assertFalse(result.error)

    def test_easy_request_preserves_vision_and_timeout(self):
        with patch.object(claude_http, "create_message", return_value=ANSWER) as create:
            self.ask()
        params, key, timeout = create.call_args.args
        self.assertEqual(key, KEY)
        self.assertEqual(timeout, config.CLAUDE_TIMEOUT_S)
        self.assertEqual(params["max_tokens"], config.CLAUDE_MAX_TOKENS)
        self.assertEqual(params["system"], ocr.SYSTEM_PROMPT)
        self.assertEqual(params["messages"][0]["content"], [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "offline-base64"}},
            {"type": "text", "text": "Read this"},
        ])
        self.assertNotIn("fallbacks", params)
        self.assertFalse(create.call_args.kwargs.get("beta"))

    def test_hard_request_preserves_effort_and_server_fallback(self):
        with patch.object(config, "CLAUDE_REFUSAL_FALLBACK", True), \
             patch.object(config, "CLAUDE_HARD_EFFORT", "high"), \
             patch.object(claude_http, "create_message", return_value=ANSWER) as create:
            self.ask(hard=True)
        params, _, timeout = create.call_args.args
        self.assertEqual(timeout, config.CLAUDE_HARD_TIMEOUT_S)
        self.assertEqual(params["max_tokens"], config.CLAUDE_HARD_MAX_TOKENS)
        self.assertEqual(params["output_config"], {"effort": "high"})
        self.assertEqual(params["fallbacks"], "default")
        self.assertEqual(create.call_args.kwargs["beta"], "server-side-fallback-2026-07-01")

    def test_disabled_server_fallback_omits_beta_and_fallback_body(self):
        with patch.object(config, "CLAUDE_REFUSAL_FALLBACK", False), \
             patch.object(config, "CLAUDE_HARD_EFFORT", ""), \
             patch.object(claude_http, "create_message", return_value=ANSWER) as create:
            self.ask(hard=True)
        self.assertNotIn("fallbacks", create.call_args.args[0])
        self.assertNotIn("output_config", create.call_args.args[0])
        self.assertFalse(create.call_args.kwargs.get("beta"))

    def test_http_errors_preserve_fatal_and_recoverable_meanings(self):
        for status, message, fatal in ((401, "invalid", True), (403, "no permission", True),
                (404, "missing model", False), (400, "Your credit balance is too low", True),
                (400, "invalid request", False), (429, "rate limited", False), (500, "offline", False)):
            with self.subTest(status=status, message=message):
                with patch.object(claude_http, "create_message",
                                  side_effect=claude_http.APIError(status, message)):
                    result = self.ask()
                self.assertTrue(result.error)
                self.assertEqual(result.fatal, fatal)
                self.assertFalse(result.quote)
                if status == 404:
                    self.assertIn("requested-model", result.error)

    def test_transport_and_invalid_responses_return_friendly_errors(self):
        with patch.object(claude_http, "create_message", side_effect=claude_http.TransportError("offline")):
            self.assertTrue(self.ask().error)
        for value in ({}, {"content": None}, {"content": "invalid"},
                      {"content": [None]}, {"content": [{"type": "text", "text": 123}]}):
            with self.subTest(value=value), \
                 patch.object(claude_http, "create_message", return_value=value):
                result = self.ask()
                self.assertTrue(result.error)
                self.assertFalse(result.quote)

    def test_missing_key_returns_fatal_without_network(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
             patch.object(claude_http, "create_message") as create:
            result = self.ask()
        self.assertTrue(result.fatal)
        self.assertTrue(result.error)
        create.assert_not_called()

    def test_text_blocks_are_combined_and_fallback_model_is_preserved(self):
        answer = {"model": "fallback-model", "stop_reason": "end_turn", "content": [
            {"type": "fallback", "to": {"model": "fallback-model"}},
            {"type": "text", "text": '{"quote":"HELLO'},
            {"type": "thinking", "thinking": "Do not include this"},
            {"type": "text", "text": ' WORLD","confidence":"high"}'},
        ]}
        with patch.object(claude_http, "create_message", return_value=answer):
            result = self.ask(hard=True)
        self.assertEqual(result.quote, "HELLO WORLD")
        self.assertEqual(result.model, "fallback-model")

    def test_refusal_and_empty_text_are_reported(self):
        for answer in ({"stop_reason": "refusal", "content": [{"type": "text", "text": "No"}]},
                       {"stop_reason": "max_tokens", "content": []}):
            with self.subTest(answer=answer), patch.object(claude_http, "create_message", return_value=answer):
                result = self.ask()
                self.assertTrue(result.error)
                self.assertFalse(result.quote)

    def test_high_confidence_easy_read_does_not_use_hard_model(self):
        with patch.object(ocr, "available", return_value=(True, "offline")), \
             patch.object(ocr, "_encode", return_value=("image/jpeg", "offline")), \
             patch.object(ocr, "ask", return_value=ocr.QuoteResult(quote="HELLO", confidence="high")) as ask:
            result = ocr.read_quote(Path("offline.png"), "dark")
        self.assertEqual(result.level, "easy")
        self.assertEqual(ask.call_count, 1)

    def test_uncertain_read_uses_hard_model_and_hint(self):
        with patch.object(ocr, "available", return_value=(True, "offline")), \
             patch.object(ocr, "_encode", return_value=("image/jpeg", "offline")), \
             patch.object(config, "CLAUDE_HARD_MODEL", "hard-model"), \
             patch.object(ocr, "ask", side_effect=[ocr.QuoteResult(quote="HALLO", confidence="low"),
                 ocr.QuoteResult(quote="HELLO", confidence="high")]) as ask:
            result = ocr.read_quote(Path("offline.png"), "dark")
        self.assertEqual(result.quote, "HELLO")
        self.assertEqual(result.level, "hard")
        self.assertEqual(ask.call_args.args[0], "hard-model")
        self.assertIn("HALLO", ask.call_args.args[3])
        self.assertTrue(ask.call_args.kwargs["hard"])

    def test_fatal_easy_error_does_not_retry_on_hard_model(self):
        with patch.object(ocr, "available", return_value=(True, "offline")), \
             patch.object(ocr, "_encode", return_value=("image/jpeg", "offline")), \
             patch.object(ocr, "ask", return_value=ocr.QuoteResult(error="invalid key", fatal=True)) as ask:
            result = ocr.read_quote(Path("offline.png"), "dark")
        self.assertTrue(result.fatal)
        self.assertEqual(ask.call_count, 1)

    def test_hard_error_keeps_easy_quote_for_manual_review(self):
        with patch.object(ocr, "available", return_value=(True, "offline")), \
             patch.object(ocr, "_encode", return_value=("image/jpeg", "offline")), \
             patch.object(config, "CLAUDE_HARD_MODEL", "hard-model"), \
             patch.object(ocr, "ask", side_effect=[ocr.QuoteResult(quote="HELLO", confidence="medium"),
                 ocr.QuoteResult(error="offline error")]):
            result = ocr.read_quote(Path("offline.png"), "dark")
        self.assertEqual(result.quote, "HELLO")
        self.assertEqual(result.confidence, "low")
        self.assertFalse(result.error)


if __name__ == "__main__":
    unittest.main()
