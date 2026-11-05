"""Offline authentication/session checks with disposable credentials and storage."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import web_auth


class WebAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("offline-password", method="pbkdf2:sha256:1000")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="mayzing_web_auth_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("MAYZING_WEB_USER", None)
        os.environ.pop("MAYZING_WEB_PASSWORD_HASH", None)
        self.auth = web_auth.WebAuth(self.root, "offline-session-secret")

    def configure(self, username="operator", password_hash=None):
        self.auth.directory.mkdir(parents=True, exist_ok=True)
        self.auth.credentials_path.write_text(json.dumps({
            "username": username, "password_hash": password_hash or self.password_hash,
        }), encoding="utf-8")

    def test_missing_and_malformed_credentials_fail_closed(self):
        self.assertIsNone(self.auth.credentials())
        self.assertEqual(self.auth.identity(), "")
        self.assertFalse(self.auth.authenticate("operator", "offline-password"))
        with self.assertRaises(ValueError):
            self.auth.create_session("a" * 32)
        self.auth.directory.mkdir()
        for payload in ("broken json", "[]", '{"username":"operator","password_hash":"plaintext"}',
                        '{"username":"operator","password_hash":"scrypt:bad$$"}'):
            self.auth.credentials_path.write_text(payload, encoding="utf-8")
            self.assertIsNone(self.auth.credentials())
            self.assertFalse(self.auth.authenticate("operator", "offline-password"))

    def test_correct_password_and_exact_username_are_required(self):
        self.configure()
        self.assertTrue(self.auth.authenticate("operator", "offline-password"))
        self.assertFalse(self.auth.authenticate("Operator", "offline-password"))
        self.assertFalse(self.auth.authenticate("operator", "wrong"))
        self.assertFalse(self.auth.authenticate("operator", ""))
        self.assertFalse(self.auth.authenticate(None, "offline-password"))
        self.assertFalse(self.auth.authenticate("operator", None))
        self.assertEqual(self.auth.credentials().username, "operator")

    def test_environment_override_is_complete_and_has_no_plaintext_fallback(self):
        self.configure()
        with patch.dict(os.environ, {"MAYZING_WEB_USER": "env-user"}):
            self.assertIsNone(self.auth.credentials())
            self.assertFalse(self.auth.authenticate("operator", "offline-password"))
        with patch.dict(os.environ, {"MAYZING_WEB_USER": "env-user",
                                     "MAYZING_WEB_PASSWORD_HASH": self.password_hash}):
            self.assertTrue(self.auth.authenticate("env-user", "offline-password"))
            self.assertFalse(self.auth.authenticate("operator", "offline-password"))
        self.configure(password_hash="pbkdf2:unsupported:1000$salt$not-a-digest")
        self.assertFalse(self.auth.authenticate("operator", "offline-password"))

    def test_session_lasts_exactly_30_days_without_refresh_on_reads(self):
        self.configure()
        start = 1_900_000_000
        with patch.object(web_auth.time, "time", return_value=start):
            token = self.auth.create_session("a" * 32)
        self.assertEqual(web_auth.SESSION_SECONDS, 30 * 86400)
        expires_at = start + web_auth.SESSION_SECONDS
        with patch.object(web_auth.time, "time", return_value=start + 86400):
            self.assertEqual(self.auth.get_session(token), {"workspace_id": "a" * 32,
                                                           "expires_at": expires_at})
        with patch.object(web_auth.time, "time", return_value=expires_at - 1):
            self.assertIsNotNone(self.auth.get_session(token))
        with patch.object(web_auth.time, "time", return_value=expires_at):
            self.assertIsNone(self.auth.get_session(token))

    def test_only_hashed_tokens_are_persisted_and_tokens_can_be_revoked(self):
        self.configure()
        first = self.auth.create_session("a" * 32)
        second = self.auth.create_session("b" * 32)
        self.assertNotEqual(first, second)
        with closing(sqlite3.connect(self.auth.database_path)) as connection:
            hashes = [row[0] for row in connection.execute("SELECT token_hash FROM sessions")]
        self.assertIn(hashlib.sha256(first.encode("ascii")).hexdigest(), hashes)
        self.assertNotIn(first, hashes)
        self.assertNotIn(first.encode("ascii"), self.auth.database_path.read_bytes())
        self.assertNotIn(b"offline-password", self.auth.database_path.read_bytes())
        self.assertIsNone(self.auth.get_session(first + "x"))
        self.assertIsNone(self.auth.get_session("!" * 43))
        self.assertIsNone(self.auth.get_session("x" * 43))
        self.auth.revoke_session(first)
        self.auth.revoke_session(first)
        self.assertIsNone(self.auth.get_session(first))
        self.assertEqual(self.auth.get_session(second)["workspace_id"], "b" * 32)

    def test_credential_or_signing_key_change_invalidates_existing_sessions(self):
        self.configure()
        token = self.auth.create_session("a" * 32)
        identity = self.auth.identity()
        self.configure(username="replacement")
        self.assertNotEqual(self.auth.identity(), identity)
        self.assertIsNone(self.auth.get_session(token))
        replacement = self.auth.create_session("b" * 32)
        self.configure(username="replacement", password_hash=generate_password_hash(
            "different-password", method="pbkdf2:sha256:1000"))
        self.assertIsNone(self.auth.get_session(replacement))
        another = self.auth.create_session("c" * 32)
        other_secret = web_auth.WebAuth(self.root, "another-secret")
        self.assertIsNone(other_secret.get_session(another))
        self.auth.credentials_path.unlink()
        self.assertIsNone(self.auth.get_session(another))

    def test_sessions_survive_new_instance_and_expired_rows_are_cleaned(self):
        self.configure()
        start = 1_900_000_000
        with patch.object(web_auth.time, "time", return_value=start):
            token = self.auth.create_session("a" * 32)
        other = web_auth.WebAuth(self.root, "offline-session-secret")
        with patch.object(web_auth.time, "time", return_value=start + 100):
            self.assertEqual(other.get_session(token)["workspace_id"], "a" * 32)
            other.revoke_session(token)
            self.assertIsNone(self.auth.get_session(token))
            old_token = self.auth.create_session("b" * 32)
        with patch.object(web_auth.time, "time", return_value=start + 101 + web_auth.SESSION_SECONDS):
            new_token = self.auth.create_session("c" * 32)
            self.assertIsNone(other.get_session(old_token))
            self.assertIsNotNone(other.get_session(new_token))
        with closing(sqlite3.connect(self.auth.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_rate_limit_ten_failures_in_fifteen_minutes_then_unlocks(self):
        with patch.object(web_auth.time, "monotonic", return_value=1000):
            for _ in range(10):
                self.assertTrue(self.auth.allow_attempt("operator"))
                self.auth.failed_attempt("operator")
            self.assertFalse(self.auth.allow_attempt("operator"))
            self.assertTrue(self.auth.allow_attempt("another"))
        with patch.object(web_auth.time, "monotonic", return_value=1899):
            self.assertFalse(self.auth.allow_attempt("operator"))
        with patch.object(web_auth.time, "monotonic", return_value=1900):
            self.assertTrue(self.auth.allow_attempt("operator"))
        self.auth.failed_attempt("operator")
        self.auth.clear_attempts("operator")
        self.assertNotIn("operator", self.auth._attempts)

    def test_rate_limit_memory_is_bounded(self):
        with patch.object(web_auth, "MAX_ATTEMPT_KEYS", 3):
            for key in ("a", "b", "c", "d"):
                self.auth.failed_attempt(key)
            self.assertEqual(len(self.auth._attempts), 3)
            for _ in range(20):
                self.auth.failed_attempt("d")
            self.assertEqual(len(self.auth._attempts["d"]), web_auth.MAX_FAILED_ATTEMPTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
