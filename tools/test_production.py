"""Offline production regression tests: isolated devices and fake browser replies.

Nothing here contacts Mayzing or changes a real shop. All device data is temporary.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import dashboard
from lib import mayzing, ocr, review
from lib.context import WORKSPACE_ID


PROD = "https://mz.eagleglobal2023.com"
EXTENSION = "chrome-extension://" + "a" * 32
READY = {"ready": True, "message": "Ready", "extensionVersion": "1.2.0"}


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="mayzing_production_test_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        for name, value in (("DEVICE_DATA_DIR", self.root / "devices"),
                            ("DRAIN_FILE", self.root / "deploy-drain"),
                            ("ARTWORK_CANVAS_SIZE", (120, 140)),
                            ("SLUG_CHECK_MAYZING", False)):
            change = patch.object(config, name, value)
            change.start()
            self.addCleanup(change.stop)
        previous_secret = dashboard.app.secret_key
        dashboard.app.secret_key = "offline-production-test-key"
        self.addCleanup(setattr, dashboard.app, "secret_key", previous_secret)
        dashboard.DEVICE_RUNTIMES.clear()
        self.addCleanup(dashboard.DEVICE_RUNTIMES.clear)
        self.a = dashboard.app.test_client()
        self.b = dashboard.app.test_client()

    def get(self, client, path):
        return client.get(path, base_url=PROD)

    def post(self, client, path, data=None, *, origin=PROD, token=None):
        headers = {"Origin": origin}
        if token:
            headers["Authorization"] = "Bearer " + token
        return client.post(path, json={} if data is None else data,
                           base_url=PROD, headers=headers)

    def info(self, client):
        response = self.get(client, "/api/info")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def runtime(self, client):
        return dashboard.DEVICE_RUNTIMES[self.info(client)["workspace_id"]]

    @contextmanager
    def scope(self, client):
        token = WORKSPACE_ID.set(self.info(client)["workspace_id"])
        try:
            yield
        finally:
            WORKSPACE_ID.reset(token)

    def pair(self, client):
        response = self.post(client, "/api/bridge/code")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        code = response.get_json()["code"]
        extension = dashboard.app.test_client(use_cookies=False)
        response = self.post(extension, "/api/bridge/pair", {"code": code}, origin=EXTENSION)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        token = response.get_json()["token"]
        response = self.post(extension, "/api/bridge/heartbeat", {"page": READY},
                             origin=EXTENSION, token=token)
        self.assertEqual(response.status_code, 200)
        return extension, token

    def enable(self, client, extension, token, store):
        """Deliver only a fake StoresMinimal reply, through the real result endpoint."""
        runtime = self.runtime(client)
        stores = store if isinstance(store, list) else [store]
        errors = []

        def browser():
            try:
                command = runtime.bridge.poll(token, READY, wait_s=2)["command"]
                if command is None or command["operation"] != "StoresMinimal":
                    raise AssertionError(f"Unexpected command: {command}")
                response = self.post(extension, "/api/bridge/result", {
                    "id": command["id"],
                    "result": {"kind": "response", "status": 200,
                               "text": json.dumps({"data": {"stores": stores}})},
                }, origin=EXTENSION, token=token)
                if response.status_code != 200:
                    raise AssertionError(response.get_data(as_text=True))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=browser, daemon=True)
        worker.start()
        original_request = runtime.bridge.request
        def short_request(*args, **kwargs):
            return original_request(*args, **(kwargs | {"timeout": 3}))
        with patch.object(runtime.bridge, "request", side_effect=short_request):
            response = self.post(client, "/api/automation/enable")
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["browser"]["automation"])
        return response.get_json()

    def upload(self, client, name="az123.png", colour=(220, 20, 20, 255)):
        image = Image.new("RGBA", (120, 140), (0, 0, 0, 0))
        image.paste(colour, (10, 10, 110, 130))
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        payload.seek(0)
        response = client.post("/api/inputs", data={"files": (payload, name)},
                               content_type="multipart/form-data", base_url=PROD,
                               headers={"Origin": PROD})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["saved"], [name])

    def finish_job(self, client, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.get(client, "/api/job")
            self.assertEqual(response.status_code, 200)
            job = response.get_json()
            if not job["running"]:
                self.assertNotIn("error", job["result"], job)
                return job
            threading.Event().wait(0.01)
        self.fail("The fake background job did not finish")

    def test_public_login_opens_on_client_and_cookie_is_private(self):
        with patch.object(mayzing, "open_chrome") as launch:
            response = self.post(self.a, "/api/login/start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["url"], config.MAYZING_DASHBOARD + "/dashboard")
        self.assertTrue(response.get_json()["client"])
        launch.assert_not_called()
        cookies = response.headers.getlist("Set-Cookie")
        self.assertTrue(any("Secure" in c and "HttpOnly" in c and "SameSite=" in c for c in cookies), cookies)

    def test_pairing_second_device_preserves_first_device_and_tokens_route_to_owner(self):
        extension_a, token_a = self.pair(self.a)
        extension_b, token_b = self.pair(self.b)
        self.assertNotEqual(token_a, token_b)
        self.assertNotEqual(self.info(self.a)["workspace_id"], self.info(self.b)["workspace_id"])
        # A bearer token selects its owner even when a different dashboard cookie is present.
        response = self.post(self.b, "/api/bridge/heartbeat", {
            "page": READY | {"message": "Only device A"}}, origin=EXTENSION, token=token_a)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.info(self.a)["browser"]["message"], "Only device A")
        self.assertEqual(self.info(self.b)["browser"]["message"], "Ready")
        response = self.post(extension_a, "/api/bridge/disconnect", origin=EXTENSION, token=token_a)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.info(self.a)["browser"]["connected"])
        self.assertTrue(self.info(self.b)["browser"]["connected"])
        self.assertEqual(self.post(extension_b, "/api/bridge/heartbeat", {"page": READY},
                                   origin=EXTENSION, token=token_b).status_code, 200)
        self.assertEqual(self.post(extension_a, "/api/bridge/heartbeat", {"page": READY},
                                   origin=EXTENSION, token=token_a).status_code, 401)

    def test_production_codes_are_single_use_and_unpaired_token_has_no_access(self):
        code_a = self.post(self.a, "/api/bridge/code").get_json()["code"]
        code_b = self.post(self.b, "/api/bridge/code").get_json()["code"]
        self.assertNotEqual(code_a, code_b)
        extension = dashboard.app.test_client(use_cookies=False)
        self.assertEqual(self.post(extension, "/api/bridge/pair", {"code": code_a},
                                   origin=EXTENSION).status_code, 200)
        self.assertEqual(self.post(extension, "/api/bridge/pair", {"code": code_a},
                                   origin=EXTENSION).status_code, 401)
        self.assertEqual(self.post(extension, "/api/bridge/heartbeat", {"page": READY},
                                   origin=EXTENSION, token="wrong").status_code, 401)
        self.assertFalse(self.info(self.b)["browser"]["connected"])
        self.assertEqual(self.post(extension, "/api/bridge/pair", {"code": code_b},
                                   origin=EXTENSION).status_code, 200)

    def test_poll_and_result_never_deliver_or_complete_another_device_command(self):
        extension_a, token_a = self.pair(self.a)
        extension_b, token_b = self.pair(self.b)
        runtime_a, runtime_b = self.runtime(self.a), self.runtime(self.b)
        replies = {}
        errors = []

        def request_read(runtime, owner):
            try:
                replies[owner] = runtime.bridge.request("StoresMinimal", {"owner": owner},
                                                        allow_disabled=True, timeout=3)
            except Exception as exc:
                errors.append(exc)

        workers = []
        for runtime, owner in ((runtime_a, "A"), (runtime_b, "B")):
            worker = threading.Thread(target=request_read, args=(runtime, owner), daemon=True)
            worker.start()
            workers.append(worker)
            with runtime.bridge.cv:
                self.assertTrue(runtime.bridge.cv.wait_for(lambda: bool(runtime.bridge.commands), timeout=1))
        command_a = self.post(extension_a, "/api/bridge/poll", {"page": READY},
                              origin=EXTENSION, token=token_a).get_json()["command"]
        command_b = self.post(extension_b, "/api/bridge/poll", {"page": READY},
                              origin=EXTENSION, token=token_b).get_json()["command"]
        self.assertEqual(command_a["payload"]["owner"], "A")
        self.assertEqual(command_b["payload"]["owner"], "B")
        result_a = {"kind": "response", "status": 200, "text": "private A"}
        result_b = {"kind": "response", "status": 200, "text": "private B"}
        # Device B cannot complete the queued command belonging to A.
        self.assertEqual(self.post(extension_b, "/api/bridge/result", {
            "id": command_a["id"], "result": result_b}, origin=EXTENSION, token=token_b).status_code, 200)
        self.assertNotIn("A", replies)
        for extension, token, command, result in ((extension_a, token_a, command_a, result_a),
                                                   (extension_b, token_b, command_b, result_b)):
            self.assertEqual(self.post(extension, "/api/bridge/result", {
                "id": command["id"], "result": result}, origin=EXTENSION, token=token).status_code, 200)
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(replies, {"A": result_a, "B": result_b})

    def test_fake_shop_enable_stores_and_disconnect_are_isolated(self):
        extension_a, token_a = self.pair(self.a)
        extension_b, token_b = self.pair(self.b)
        store_a = {"slug": "SHOP-A", "name": "Shop A", "domainName": "a.example"}
        store_b = {"slug": "SHOP-B", "name": "Shop B", "domainName": "b.example"}
        self.enable(self.a, extension_a, token_a, store_a)
        self.enable(self.b, extension_b, token_b, store_b)
        self.assertEqual(self.info(self.a)["session"]["stores"], [store_a])
        self.assertEqual(self.info(self.b)["session"]["stores"], [store_b])
        self.assertEqual(self.info(self.a)["settings"]["store_slug"], "SHOP-A")
        self.assertEqual(self.info(self.b)["settings"]["store_slug"], "SHOP-B")
        self.assertIn(self.post(self.b, "/api/settings", {"store_slug": "SHOP-A"}).status_code, (400, 403))
        self.assertEqual(self.info(self.b)["settings"]["store_slug"], "SHOP-B")
        self.assertEqual(self.post(self.a, "/api/automation/disable").status_code, 200)
        self.assertFalse(self.info(self.a)["browser"]["automation"])
        self.assertTrue(self.info(self.b)["browser"]["automation"])

    def prepare_old_shop(self):
        extension, token = self.pair(self.a)
        self.enable(self.a, extension, token, {
            "slug": "OLD", "name": "Old shop", "domainName": "old.example"})
        with self.scope(self.a):
            config.save_settings({
                "origin_default": "old-default", "origin_dark": "old-dark", "origin_light": "old-light",
                "default_products": {"old-default": {"base_product_id": "old-product", "label": "Old"}},
            })
        runtime = self.runtime(self.a)
        runtime.campaigns["OLD"] = (time.time(), [{"id": "old-campaign"}])
        runtime.product_names["OLD"] = (time.time(), {"old-product": "Old"})
        return token

    def assert_old_shop_cleared(self):
        settings = self.info(self.a)["settings"]
        for name in ("origin_default", "origin_dark", "origin_light"):
            self.assertEqual(settings[name], "", name)
        self.assertEqual(settings["default_products"], {})
        self.assertEqual(self.runtime(self.a).campaigns, {})
        self.assertEqual(self.runtime(self.a).product_names, {})

    def test_repair_to_different_single_shop_clears_old_origins_products_and_caches(self):
        old_token = self.prepare_old_shop()
        extension, token = self.pair(self.a)
        self.assertNotEqual(old_token, token)
        self.assertEqual(self.post(extension, "/api/bridge/heartbeat", {"page": READY},
                                   origin=EXTENSION, token=old_token).status_code, 401)
        new_store = {"slug": "NEW", "name": "New shop", "domainName": "new.example"}
        response = self.enable(self.a, extension, token, new_store)
        self.assertEqual(response["settings"]["store_slug"], "NEW")
        self.assertEqual(response["settings"]["store_name"], "New shop")
        self.assertEqual(response["settings"]["store_domain"], "new.example")
        self.assert_old_shop_cleared()

    def test_repair_to_new_multiple_shops_requires_selection_and_clears_previous_shop(self):
        self.prepare_old_shop()
        extension, token = self.pair(self.a)
        new_stores = [{"slug": "NEW-B", "name": "Shop B", "domainName": "b.example"},
                      {"slug": "NEW-C", "name": "Shop C", "domainName": "c.example"}]
        response = self.enable(self.a, extension, token, new_stores)
        for name in ("store_slug", "store_name", "store_domain"):
            self.assertEqual(response["settings"][name], "", name)
        self.assertEqual(self.info(self.a)["session"]["stores"], new_stores)
        self.assert_old_shop_cleared()
        response = self.post(self.a, "/api/settings", {"store_slug": "NEW-C"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["settings"]["store_domain"], "c.example")

    def test_settings_and_campaign_cache_are_isolated_even_for_same_shop_slug(self):
        for client, owner in ((self.a, "A"), (self.b, "B")):
            runtime = self.runtime(client)
            runtime.session.update(ok=True, error="", stores=[{"slug": "SHARED", "name": owner}])
            response = self.post(client, "/api/settings", {"store_slug": "SHARED", "extra_tags": [owner]})
            self.assertEqual(response.status_code, 200)
            runtime.campaigns["SHARED"] = (time.time(), [{
                "id": "campaign-" + owner, "name": owner, "slug": owner.lower(), "productIds": [],
            }])
        for client, owner in ((self.a, "A"), (self.b, "B")):
            self.assertEqual(self.info(client)["settings"]["extra_tags"], [owner])
            response = self.get(client, "/api/campaigns")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([g["id"] for g in response.get_json()["campaigns"]], ["campaign-" + owner])

    def test_artwork_review_export_batches_and_logs_are_private(self):
        self.upload(self.a, colour=(220, 20, 20, 255))
        self.upload(self.b, colour=(20, 20, 220, 255))
        for client, expected in ((self.a, (220, 20, 20, 255)), (self.b, (20, 20, 220, 255))):
            response = self.get(client, "/img/input/az123.png")
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["Cache-Control"])
            with Image.open(io.BytesIO(response.data)) as image:
                self.assertEqual(image.getpixel((30, 30)), expected)
            response.close()
        self.assertEqual(self.post(self.a, "/api/prep/start", {"ocr": False, "origin": "origin-a"}).status_code, 200)
        self.finish_job(self.a)
        cards = self.get(self.a, "/api/items").get_json()["items"]
        self.assertEqual(len(cards), 1)
        uid = cards[0]["uid"]
        self.assertEqual(self.get(self.b, "/api/items").get_json()["items"], [])
        self.assertEqual(self.post(self.b, f"/api/item/{uid}", {"title": "Other user"}).status_code, 404)
        self.assertEqual(self.get(self.b, f"/img/thumb/{uid}").status_code, 404)
        response = self.post(self.a, f"/api/item/{uid}", {"title": "MY DESIGN", "decision": "approved"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        response = self.post(self.a, "/api/export")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        batch = response.get_json()["batch"]
        self.assertEqual(len(self.get(self.a, "/api/batches").get_json()["batches"]), 1)
        self.assertEqual(self.get(self.b, "/api/batches").get_json()["batches"], [])
        self.assertEqual(self.get(self.b, f"/img/batch/{batch}/az123.png").status_code, 404)
        self.assertEqual(self.post(self.b, f"/api/batch/{batch}/delete").status_code, 404)
        with self.scope(self.a):
            (config.LOG_DIR / f"{batch}.log").write_text("Private upload log A", encoding="utf-8")
        self.assertEqual(self.get(self.a, f"/api/batch/{batch}/log").get_json()["log"], "Private upload log A")
        self.assertEqual(self.get(self.b, f"/api/batch/{batch}/log").get_json()["log"], "")
        self.assertEqual([f["name"] for f in self.get(self.b, "/api/inputs").get_json()["files"]], ["az123.png"])

    def test_two_background_jobs_keep_own_workspace_after_request_ends(self):
        workspace_a = self.info(self.a)["workspace_id"]
        workspace_b = self.info(self.b)["workspace_id"]
        self.post(self.a, "/api/settings", {"extra_tags": ["A"]})
        self.post(self.b, "/api/settings", {"extra_tags": ["B"]})
        seen = {}
        lock = threading.Lock()
        both_started = threading.Event()
        release = threading.Event()

        def fake_prep(instance, say, **kwargs):
            owner = WORKSPACE_ID.get()
            with lock:
                seen[owner] = (str(config.INPUT_DIR), config.load_settings()["extra_tags"])
                if len(seen) == 2:
                    both_started.set()
            say("Private job for " + owner)
            if not release.wait(3):
                raise AssertionError("Both independent jobs did not start")
            # Resolve paths again after the HTTP requests have ended.
            self.assertIn(owner, str(config.INPUT_DIR))
            return 1

        with patch.object(review.Review, "run_prep", autospec=True, side_effect=fake_prep):
            try:
                self.assertEqual(self.post(self.a, "/api/prep/start", {"ocr": False}).status_code, 200)
                self.assertEqual(self.post(self.b, "/api/prep/start", {"ocr": False}).status_code, 200)
                self.assertTrue(both_started.wait(2))
                self.assertTrue(self.get(self.a, "/api/job").get_json()["running"])
                self.assertTrue(self.get(self.b, "/api/job").get_json()["running"])
            finally:
                release.set()
            job_a = self.finish_job(self.a)
            job_b = self.finish_job(self.b)
        self.assertEqual(set(seen), {workspace_a, workspace_b})
        self.assertEqual(seen[workspace_a][1], ["A"])
        self.assertEqual(seen[workspace_b][1], ["B"])
        self.assertNotEqual(seen[workspace_a][0], seen[workspace_b][0])
        self.assertIn(workspace_a, "\n".join(job_a["lines"]))
        self.assertNotIn(workspace_b, "\n".join(job_a["lines"]))
        self.assertIn(workspace_b, "\n".join(job_b["lines"]))
        self.assertNotIn(workspace_a, "\n".join(job_b["lines"]))

    def test_parallel_ocr_workers_inherit_device_workspace(self):
        workspace = self.info(self.a)["workspace_id"]
        self.post(self.a, "/api/settings", {"extra_tags": ["OCR-A"]})
        self.upload(self.a, "az123.png")
        self.upload(self.a, "az124.png")
        seen = []
        barrier = threading.Barrier(2)

        def fake_quote(path, tone):
            seen.append((WORKSPACE_ID.get(), str(config.WORK_DIR), config.load_settings()["extra_tags"]))
            barrier.wait(timeout=2)
            return ocr.QuoteResult(quote="MY TEST DESIGN", confidence="high", model="fake", level="easy")

        with patch.object(ocr, "available", return_value=(True, "fake OCR")), \
             patch.object(ocr, "read_quote", side_effect=fake_quote):
            self.assertEqual(self.post(self.a, "/api/prep/start", {"ocr": True}).status_code, 200)
            self.finish_job(self.a)
        self.assertEqual(len(seen), 2)
        for owner, directory, tags in seen:
            self.assertEqual(owner, workspace)
            self.assertIn(workspace, directory)
            self.assertEqual(tags, ["OCR-A"])
        cards = self.get(self.a, "/api/items").get_json()["items"]
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(i["quote"] == "MY TEST DESIGN" and not i["ocr_error"] for i in cards))
        self.assertEqual(self.get(self.b, "/api/items").get_json()["items"], [])

    def test_cross_origin_posts_are_rejected_before_mutating_any_device_state(self):
        initial = self.info(self.a)["workspace_id"]
        for path in ("/api/settings", "/api/bridge/code", "/api/automation/enable", "/api/automation/disable",
                     "/api/login/start", "/api/prep/start", "/api/items/discard", "/api/export",
                     "/api/inputs/delete-all", "/api/upload/start", "/api/publish"):
            for origin in ("https://untrusted.example", PROD + ".attacker.example", "null"):
                with self.subTest(path=path, origin=origin):
                    response = self.post(self.a, path, {"extra_tags": ["attacker"]}, origin=origin)
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.info(self.a)["workspace_id"], initial)
        self.assertEqual(self.info(self.a)["settings"]["extra_tags"], [])
        self.assertFalse(self.get(self.a, "/api/job").get_json()["running"])
        with patch.object(mayzing, "open_chrome") as launch:
            response = self.a.post("/api/login/start", json={}, base_url="https://attacker.example",
                                   headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        launch.assert_not_called()

    def test_missing_or_tampered_cookie_never_falls_back_to_local_or_another_device(self):
        self.post(self.a, "/api/settings", {"extra_tags": ["private-a"]})
        owner = self.info(self.a)["workspace_id"]
        anonymous = dashboard.app.test_client()
        empty = self.info(anonymous)
        self.assertNotEqual(empty["workspace_id"], owner)
        self.assertEqual(empty["settings"]["extra_tags"], [])
        self.assertFalse(empty["browser"]["connected"])
        name = dashboard.app.config["SESSION_COOKIE_NAME"]
        forged = dashboard.app.test_client()
        forged.set_cookie(name, owner, domain="mz.eagleglobal2023.com")
        info = self.info(forged)
        self.assertNotEqual(info["workspace_id"], owner)
        self.assertNotEqual(info["workspace_id"], "")
        self.assertEqual(info["settings"]["extra_tags"], [])
        self.assertEqual(self.info(self.a)["settings"]["extra_tags"], ["private-a"])

    def test_health_sets_no_cookie_and_counts_jobs_across_every_runtime(self):
        anonymous = dashboard.app.test_client()
        response = self.get(anonymous, "/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(dashboard.DEVICE_RUNTIMES, {})
        self.assertEqual(response.get_json()["active_jobs"], 0)
        self.assertEqual(response.get_json()["active_requests"], 0)
        runtime_a, runtime_b = self.runtime(self.a), self.runtime(self.b)
        with patch.object(runtime_a.job, "running", True), \
             patch.object(runtime_b.job, "running", True), \
             patch.object(dashboard._default_job, "running", True):
            response = self.get(anonymous, "/api/health")
            self.assertEqual(response.get_json()["active_jobs"], 3)
            self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(self.get(anonymous, "/api/health").get_json()["active_jobs"], 0)

    def test_health_tracks_an_unsafe_synchronous_request_until_it_finishes(self):
        self.info(self.a)
        started, release = threading.Event(), threading.Event()
        responses = []
        original_save = config.save_settings

        def blocking_save(changes):
            started.set()
            if not release.wait(3):
                raise AssertionError("The synchronous request was not released")
            return original_save(changes)

        def request_settings():
            responses.append(self.post(self.a, "/api/settings", {"extra_tags": ["sync"]}))

        worker = threading.Thread(target=request_settings, daemon=True)
        with patch.object(config, "save_settings", side_effect=blocking_save):
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                response = self.get(self.b, "/api/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["active_requests"], 1)
                self.assertEqual(response.get_json()["active_jobs"], 0)
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(self.get(self.b, "/api/health").get_json()["active_requests"], 0)

    def test_deploy_drain_blocks_new_work_but_allows_existing_bridge_results(self):
        extension, token = self.pair(self.a)
        runtime = self.runtime(self.a)
        replies, errors = [], []

        def request_read():
            try:
                replies.append(runtime.bridge.request("StoresMinimal", {}, allow_disabled=True, timeout=3))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=request_read, daemon=True)
        worker.start()
        with runtime.bridge.cv:
            self.assertTrue(runtime.bridge.cv.wait_for(lambda: bool(runtime.bridge.commands), timeout=1))
        command = runtime.bridge.poll(token, READY, wait_s=0)["command"]
        config.DRAIN_FILE.write_text("offline test drain", encoding="ascii")
        self.assertEqual(self.post(self.a, "/api/prep/start", {"ocr": False}).status_code, 503)
        self.assertEqual(self.post(self.a, "/api/bridge/code").status_code, 503)
        self.assertFalse(runtime.job.running)
        self.assertFalse(dashboard.Job().start("new", lambda: {"unexpected": True}))
        self.assertEqual(self.post(extension, "/api/bridge/heartbeat", {"page": READY},
                                   origin=EXTENSION, token=token).status_code, 200)
        result = {"kind": "response", "status": 200, "text": '{"data":{"stores":[]}}'}
        self.assertEqual(self.post(extension, "/api/bridge/result", {
            "id": command["id"], "result": result}, origin=EXTENSION, token=token).status_code, 200)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(replies, [result])
        response = self.get(self.b, "/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(response.get_json()["active_requests"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
