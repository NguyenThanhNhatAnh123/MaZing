"""Minimal Messages API transport without native SDK/parser dependencies."""
from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(f"HTTP {status}: {message}")


class TransportError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API credentials must never follow a Location header to another host.
        return None


def _retry_delay(headers, attempt: int) -> float:
    try:
        if headers.get("retry-after-ms") is not None:
            delay = float(headers["retry-after-ms"]) / 1000
        elif headers.get("retry-after") is not None:
            value = headers["retry-after"]
            try:
                delay = float(value)
            except ValueError:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
        else:
            delay = 0.5 * 2 ** attempt
        if math.isfinite(delay) and 0 <= delay <= 60:
            return delay
    except (ValueError, TypeError, OverflowError):
        pass
    return 0.5 * 2 ** attempt


def _error_message(body: bytes, status: int, api_key: str) -> str:
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
    except (ValueError, UnicodeError):
        message = None
    text = str(message or f"HTTP {status}")
    return text.replace(api_key, "[redacted]")[:500] if api_key else text[:500]


def create_message(params: dict, api_key: str, timeout: float, beta: str = "") -> dict:
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "anthropic-version": API_VERSION,
               "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "MaZing-OCR/1.0"}
    if beta:
        headers["anthropic-beta"] = beta
    opener = urllib.request.build_opener(_NoRedirect())
    for attempt in range(3):
        req = urllib.request.Request(MESSAGES_URL, data=body, headers=headers, method="POST")
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TransportError("Phản hồi Claude quá lớn.")
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise TransportError("Claude trả dữ liệu không hợp lệ.") from exc
            if not isinstance(payload, dict) or payload.get("type") == "error":
                raise TransportError("Claude trả dữ liệu không hợp lệ.")
            return payload
        except urllib.error.HTTPError as exc:
            status, response_headers = exc.code, exc.headers or {}
            try:
                message = _error_message(exc.read(MAX_RESPONSE_BYTES), status, api_key)
            finally:
                exc.close()
            hint = response_headers.get("x-should-retry")
            retry = hint == "true" or (hint != "false" and (status in (408, 409, 429) or status >= 500))
            if attempt < 2 and retry and not 300 <= status < 400:
                time.sleep(_retry_delay(response_headers, attempt))
                continue
            raise APIError(status, message) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
            if attempt < 2:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise TransportError(f"Lỗi kết nối Claude ({type(exc).__name__}).") from None
    raise TransportError("Không nhận được phản hồi Claude.")
