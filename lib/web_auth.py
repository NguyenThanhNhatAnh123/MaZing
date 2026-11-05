"""Persistent dashboard login sessions, separate from browser/shop credentials."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from werkzeug.security import check_password_hash


SESSION_SECONDS = 30 * 86400
ATTEMPT_WINDOW_SECONDS = 15 * 60
MAX_FAILED_ATTEMPTS = 10
MAX_ATTEMPT_KEYS = 4096


@dataclass(frozen=True)
class Credentials:
    username: str
    password_hash: str


class WebAuth:
    """Keep revocable, fixed-lifetime sessions outside immutable releases."""

    def __init__(self, data_root: Path, secret: str):
        self.directory = Path(data_root) / "auth"
        self.credentials_path = self.directory / "web-login.json"
        self.database_path = self.directory / "web-sessions.sqlite3"
        self._secret = secret.encode("utf-8")
        self._database_lock = threading.RLock()
        self._attempt_lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def credentials(self) -> Credentials | None:
        """Environment overrides must contain both values; no fallback password."""
        username = os.environ.get("MAYZING_WEB_USER")
        password_hash = os.environ.get("MAYZING_WEB_PASSWORD_HASH")
        if username is None and password_hash is None:
            try:
                raw = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                return None
            if not isinstance(raw, dict):
                return None
            username, password_hash = raw.get("username"), raw.get("password_hash")
        if not isinstance(username, str) or not isinstance(password_hash, str):
            return None
        if not username or username != username.strip() or len(username) > 254:
            return None
        # Werkzeug's supported password hashes contain method, salt and digest.
        # Reject plaintext and empty/malformed entries without ever authenticating.
        if not 1 <= len(password_hash) <= 4096 or len(password_hash.split("$")) != 3:
            return None
        method, salt, digest = password_hash.split("$")
        if not (method.startswith("scrypt:") or method.startswith("pbkdf2:")) or not salt or not digest:
            return None
        return Credentials(username=username, password_hash=password_hash)

    def _identity(self, credentials: Credentials) -> str:
        payload = json.dumps([credentials.username, credentials.password_hash],
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._secret, b"mayzing-web-auth\x00" + payload, hashlib.sha256).hexdigest()

    def identity(self) -> str:
        credentials = self.credentials()
        return self._identity(credentials) if credentials is not None else ""

    def authenticate(self, username: str, password: str) -> bool:
        credentials = self.credentials()
        if credentials is None or not isinstance(username, str) or not isinstance(password, str):
            return False
        if len(username) > 254 or not password or len(password) > 4096:
            return False
        try:
            # Verify even for an incorrect username to avoid an account-name timing oracle.
            valid_password = check_password_hash(credentials.password_hash, password)
        except (ValueError, TypeError, OverflowError):
            return False
        return hmac.compare_digest(username.encode("utf-8"), credentials.username.encode("utf-8")) and valid_password

    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                credential_identity TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )""")
            connection.commit()
            self.database_path.chmod(0o600)
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _token_hash(token: str) -> str | None:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def create_session(self, workspace_id: str) -> str:
        credential_identity = self.identity()
        if not credential_identity:
            raise ValueError("Dashboard login credentials are not configured.")
        if not isinstance(workspace_id, str) or not re.fullmatch(r"[a-f0-9]{32}", workspace_id):
            raise ValueError("Invalid dashboard workspace ID.")
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._database_lock:
            connection = self._connect()
            try:
                with connection:
                    connection.execute("DELETE FROM sessions WHERE expires_at <= ? OR credential_identity != ?",
                                       (now, credential_identity))
                    connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                                       (self._token_hash(token), workspace_id, credential_identity,
                                        now + SESSION_SECONDS))
            finally:
                connection.close()
        return token

    def get_session(self, token: str) -> dict | None:
        token_hash = self._token_hash(token)
        credential_identity = self.identity()
        if token_hash is None or not credential_identity or not self.database_path.exists():
            return None
        with self._database_lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT workspace_id, credential_identity, expires_at FROM sessions WHERE token_hash = ?",
                    (token_hash,)).fetchone()
            finally:
                connection.close()
        if row is None or row[2] <= int(time.time()) or not hmac.compare_digest(row[1], credential_identity):
            return None
        return {"workspace_id": row[0], "expires_at": row[2]}

    def revoke_session(self, token: str) -> None:
        token_hash = self._token_hash(token)
        if token_hash is None or not self.database_path.exists():
            return
        with self._database_lock:
            connection = self._connect()
            try:
                with connection:
                    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            finally:
                connection.close()

    def _prune_attempts(self, now: float) -> None:
        cutoff = now - ATTEMPT_WINDOW_SECONDS
        for key, failures in list(self._attempts.items()):
            remaining = [stamp for stamp in failures if stamp > cutoff]
            if remaining:
                self._attempts[key] = remaining
            else:
                del self._attempts[key]

    def allow_attempt(self, key: str) -> bool:
        with self._attempt_lock:
            self._prune_attempts(time.monotonic())
            return len(self._attempts.get(key, ())) < MAX_FAILED_ATTEMPTS

    def failed_attempt(self, key: str) -> None:
        now = time.monotonic()
        with self._attempt_lock:
            self._prune_attempts(now)
            if key not in self._attempts and len(self._attempts) >= MAX_ATTEMPT_KEYS:
                oldest = min(self._attempts, key=lambda item: self._attempts[item][-1])
                del self._attempts[oldest]
            failures = self._attempts.setdefault(key, [])
            failures.append(now)
            del failures[:-MAX_FAILED_ATTEMPTS]

    def clear_attempts(self, key: str) -> None:
        with self._attempt_lock:
            self._attempts.pop(key, None)
