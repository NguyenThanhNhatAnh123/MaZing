"""Offline dashboard login checks using disposable accounts and browser sessions."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Importing the dashboard must not load an operator's environment or signing key.
_IMPORT_FOLDER = tempfile.TemporaryDirectory(prefix="mayzing_login_import_")
with patch.dict(os.environ, {
    "MAYZING_DATA_ROOT": _IMPORT_FOLDER.name,
    "MAYZING_ENV_FILE": str(Path(_IMPORT_FOLDER.name) / "absent.env"),
    "MAYZING_SESSION_SECRET": "offline-login-signing-secret-for-tests-only",
}):
    import config
    import dashboard
from lib.browser_bridge import Command
from lib.web_auth import SESSION_SECONDS, WebAuth


PROD = "https://mz.eagleglobal2023.com"
HOST = "mz.eagleglobal2023.com"
EXTENSION = "chrome-extension://" + "a" * 32


class LoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("offline-login-password", method="pbkdf2:sha256:1000")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="mayzing_login_test_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("MAYZING_WEB_USER", None)
        os.environ.pop("MAYZING_WEB_PASSWORD_HASH", None)
        self.auth = WebAuth(self.root, "offline-login-signing-secret-for-tests-only")
        self.configure()
        for target, name, value in (
                (dashboard, "WEB_AUTH", self.auth),
                (config, "DEVICE_DATA_DIR", self.root / "devices"),
                (config, "DRAIN_FILE", self.root / "deploy-drain")):
            change = patch.object(target, name, value)
            change.start()
            self.addCleanup(change.stop)
        app_config = patch.dict(dashboard.app.config, {
            "TESTING": True, "REQUIRE_LOCAL_LOGIN": False,
            "SECRET_KEY": "offline-login-signing-secret-for-tests-only",
        })
        app_config.start()
        self.addCleanup(app_config.stop)
        dashboard.DEVICE_RUNTIMES.clear()
        self.addCleanup(dashboard.DEVICE_RUNTIMES.clear)
        self.client = dashboard.app.test_client()

    def configure(self, username="operator", password_hash=None):
        self.auth.directory.mkdir(parents=True, exist_ok=True)
        self.auth.credentials_path.write_text(json.dumps({
            "username": username, "password_hash": password_hash or self.password_hash,
        }), encoding="utf-8")

    def get(self, path, client=None, **kwargs):
        response = (client or self.client).get(path, base_url=PROD, **kwargs)
        self.addCleanup(response.close)
        return response

    def csrf(self, client=None):
        with (client or self.client).session_transaction(base_url=PROD) as session:
            return session["csrf_token"]

    def login(self, client=None, *, password="offline-login-password", next_path="/"):
        client = client or self.client
        response = self.get("/login", client)
        self.assertEqual(response.status_code, 200)
        return client.post("/login", base_url=PROD, headers={"Origin": PROD}, data={
            "username": "operator", "password": password,
            "csrf_token": self.csrf(client), "next": next_path,
        })

    def info(self, client=None):
        response = self.get("/api/info", client)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def logout(self, client=None, **kwargs):
        client = client or self.client
        return client.post("/logout", base_url=PROD, headers={"Origin": PROD},
                           json={"csrf_token": self.info(client)["auth"]["csrf_token"]}, **kwargs)

    def cookie(self, name, client=None):
        return (client or self.client).get_cookie(name, domain=HOST)

    def assert_denied(self, client=None):
        response = self.get("/api/info", client)
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.get_json()["auth_required"])
        self.assertEqual(response.get_json()["login"], "/login")

    def test_pages_redirect_and_apis_deny_without_allocating_workspace(self):
        response = self.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.startswith("/login?next="))
        self.assert_denied()
        response = self.client.post("/api/bridge/code", base_url=PROD,
                                    headers={"Origin": PROD}, json={})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(dashboard.DEVICE_RUNTIMES)
        self.assertFalse((self.root / "devices").exists())

    def test_guide_health_download_and_login_are_public_without_workspace(self):
        for path in ("/api/health", "/api/huongdan", "/api/huongdan/",
                     "/api/huongdan/extension.zip", "/login"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 200)
        with self.client.session_transaction(base_url=PROD) as session:
            self.assertIn("csrf_token", session)
            self.assertNotIn("workspace_id", session)
        self.assertFalse(dashboard.DEVICE_RUNTIMES)
        self.assertFalse((self.root / "devices").exists())

    def test_success_sets_secure_opaque_cookie_for_thirty_days_and_rotates_csrf(self):
        self.get("/login")
        old_csrf = self.csrf()
        response = self.login(next_path="/?view=mayzing")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/?view=mayzing")
        cookies = SimpleCookie()
        for header in response.headers.getlist("Set-Cookie"):
            cookies.load(header)
        login = cookies["mayzing_login"]
        self.assertEqual(login["max-age"], str(30 * 86400))
        self.assertEqual(login["path"], "/")
        self.assertTrue(login["secure"])
        self.assertTrue(login["httponly"])
        self.assertEqual(login["samesite"], "Lax")
        self.assertRegex(login.value, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotIn("offline-login-password", login.value)
        self.assertNotEqual(old_csrf, self.csrf())
        self.assertIsNotNone(self.cookie("mayzing_device"))
        self.assertEqual(self.get("/").status_code, 200)
        self.assertTrue(self.info()["auth"]["enabled"])

    def test_password_change_during_verification_cannot_create_a_new_session(self):
        original = self.auth.authenticate

        def changing_account(username, password):
            result = original(username, password)
            self.configure(username="replacement-account")
            return result

        with patch.object(self.auth, "authenticate", side_effect=changing_account):
            response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(self.cookie("mayzing_login"))
        self.assert_denied()

    def test_wrong_password_does_not_create_authentication_or_workspace(self):
        self.assertEqual(self.login(password="wrong-password").status_code, 401)
        self.assertIsNone(self.cookie("mayzing_login"))
        self.assert_denied()
        self.assertFalse(dashboard.DEVICE_RUNTIMES)

    def test_login_rejects_missing_wrong_and_another_browsers_csrf(self):
        self.get("/login")
        another = dashboard.app.test_client()
        self.get("/login", another)
        for token in (None, "invalid", self.csrf(another)):
            with self.subTest(token=token):
                data = {"username": "operator", "password": "offline-login-password"}
                if token is not None:
                    data["csrf_token"] = token
                response = self.client.post("/login", base_url=PROD,
                                            headers={"Origin": PROD}, data=data)
                self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.cookie("mayzing_login"))

    def test_login_rejects_cross_origin_and_missing_origin(self):
        self.get("/login")
        data = {"username": "operator", "password": "offline-login-password", "csrf_token": self.csrf()}
        for headers in ({}, {"Origin": "https://attacker.example"}, {"Origin": EXTENSION}):
            with self.subTest(headers=headers):
                response = self.client.post("/login", base_url=PROD, headers=headers, data=data)
                self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.cookie("mayzing_login"))

    def test_unsafe_redirects_fall_back_to_dashboard(self):
        for path in ("https://attacker.example/", "//attacker.example/", "/\\attacker.example/",
                     "/inside\\outside", "/login", "/logout", "/bad\x00path"):
            with self.subTest(path=path):
                client = dashboard.app.test_client()
                response = self.login(client, next_path=path)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.location, "/")

    def test_authentication_cookie_tampering_and_server_expiry_are_rejected(self):
        self.assertEqual(self.login().status_code, 303)
        token = self.cookie("mayzing_login").value
        changed = ("A" if token[0] != "A" else "B") + token[1:]
        self.client.set_cookie("mayzing_login", changed, domain=HOST)
        self.assert_denied()
        self.client.set_cookie("mayzing_login", token, domain=HOST)
        self.assertEqual(self.get("/api/info").status_code, 200)
        with closing(sqlite3.connect(self.auth.database_path)) as database:
            with database:
                database.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
        self.assert_denied()

    def test_server_restart_preserves_session_but_does_not_extend_deadline(self):
        self.login()
        token = self.cookie("mayzing_login").value
        before = self.auth.get_session(token)
        restarted = WebAuth(self.root, "offline-login-signing-secret-for-tests-only")
        dashboard.DEVICE_RUNTIMES.clear()
        with patch("lib.web_auth.time.time", return_value=int(time.time()) + 90):
            with patch.object(dashboard, "WEB_AUTH", restarted):
                self.assertEqual(self.info()["workspace_id"], before["workspace_id"])
            after = restarted.get_session(token)
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertLessEqual(after["expires_at"], int(time.time()) + SESSION_SECONDS)

    def test_missing_credentials_fail_closed(self):
        self.auth.credentials_path.unlink()
        response = self.get("/login")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Chưa cấu hình tài khoản", response.get_data(as_text=True))
        self.assertIsNone(self.cookie("mayzing_login"))
        self.assert_denied()
        self.assertFalse(dashboard.DEVICE_RUNTIMES)

    def test_account_change_invalidates_existing_authentication(self):
        self.login()
        self.configure(username="replacement-operator")
        self.assert_denied()

    def test_same_account_browsers_have_separate_workspaces_and_copied_device_cookie_cannot_switch_them(self):
        other = dashboard.app.test_client()
        self.login()
        first = self.info()["workspace_id"]
        self.login(other)
        second = self.info(other)["workspace_id"]
        self.assertNotEqual(first, second)
        device_cookie = self.cookie("mayzing_device", other)
        self.client.set_cookie("mayzing_device", device_cookie.value, domain=HOST)
        self.assertEqual(self.info()["workspace_id"], first)
        self.client.delete_cookie("mayzing_device", domain=HOST)
        self.assertEqual(self.info()["workspace_id"], first)

    def test_logout_requires_valid_csrf_and_same_origin(self):
        self.login()
        token = self.cookie("mayzing_login").value
        for body, headers in (({}, {"Origin": PROD}),
                              ({"csrf_token": "wrong"}, {"Origin": PROD}),
                              ({"csrf_token": self.csrf()}, {"Origin": "https://attacker.example"})):
            with self.subTest(body=body, headers=headers):
                response = self.client.post("/logout", base_url=PROD, headers=headers, json=body)
                self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.auth.get_session(token))
        self.assertIn(self.get("/logout").status_code, (404, 405))
        self.assertIsNotNone(self.auth.get_session(token))

    def test_logout_revokes_replay_disconnects_extension_and_preserves_workspace_for_relogin(self):
        self.login()
        workspace = self.info()["workspace_id"]
        login_token = self.cookie("mayzing_login").value
        runtime = dashboard.DEVICE_RUNTIMES[workspace]
        code = runtime.bridge.pairing_code()["code"]
        extension_token = runtime.bridge.pair(code)["token"]
        runtime.bridge.pairing_code()
        response = self.logout()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.cookie("mayzing_login"))
        self.assertIsNone(self.auth.get_session(login_token))
        self.assertEqual(runtime.bridge.token, "")
        self.assertEqual(runtime.bridge.code, "")
        extension = dashboard.app.test_client(use_cookies=False)
        response = extension.post("/api/bridge/heartbeat", base_url=PROD,
                                  headers={"Origin": EXTENSION, "Authorization": "Bearer " + extension_token},
                                  json={"page": {"ready": True}})
        self.assertEqual(response.status_code, 401)
        self.client.set_cookie("mayzing_login", login_token, domain=HOST)
        self.assert_denied()
        self.client.delete_cookie("mayzing_login", domain=HOST)
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.info()["workspace_id"], workspace)

    def test_logout_waits_for_active_job_or_bridge_command_without_revoking_session(self):
        self.login()
        token = self.cookie("mayzing_login").value
        runtime = dashboard.DEVICE_RUNTIMES[self.info()["workspace_id"]]
        runtime.job.running = True
        try:
            self.assertEqual(self.logout().status_code, 409)
        finally:
            runtime.job.running = False
        self.assertIsNotNone(self.auth.get_session(token))
        command = Command("StoresMinimal", {}, False)
        runtime.bridge.commands[command.id] = command
        try:
            self.assertEqual(self.logout().status_code, 409)
        finally:
            runtime.bridge.commands.clear()
        self.assertIsNotNone(self.auth.get_session(token))
        self.assertEqual(self.logout().status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
