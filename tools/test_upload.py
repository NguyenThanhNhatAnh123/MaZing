"""Regression tests for multi-task clone replies and recovery without duplicate campaigns."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import upload_batch
from lib import manifest, mayzing


class CloneReplyTests(unittest.TestCase):
    def test_task_error_reconciliation_requires_exact_unique_names(self):
        rows = [{"title": "A"}, {"title": "B"}]
        self.assertEqual(upload_batch.match_new_groups(rows,
            [{"id": "a", "name": "A (1)"}, {"id": "b", "name": "B"}], exact_only=True),
            {1: {"id": "b", "name": "B"}})
        self.assertEqual(upload_batch.match_new_groups([{"title": "A"}],
            [{"id": "a1", "name": "A"}, {"id": "a2", "name": "a"}], exact_only=True), {})
        self.assertEqual(upload_batch.match_new_groups([{"title": "A"}, {"title": "a"}],
            [{"id": "a1", "name": "A"}], exact_only=True), {})

    def test_all_returned_tasks_are_preserved(self):
        client = mayzing.Mayzing()
        tasks = [{"id": "task-1"}, {"id": "task-2"}]
        client.gql = Mock(return_value={"cloneGroup": tasks})
        self.assertEqual(client.clone_group("origin", ["image-1", "image-2"]), tasks)
        client.gql.assert_called_once()

    def test_single_object_reply_remains_supported(self):
        client = mayzing.Mayzing()
        client.gql = Mock(return_value={"cloneGroup": {"id": "task-1"}})
        self.assertEqual(client.clone_group("origin", ["image-1"]), [{"id": "task-1"}])

    def test_invalid_reply_is_not_retried(self):
        for value in (None, [], [None], [{"status": "PENDING"}], "bad"):
            with self.subTest(value=value):
                client = mayzing.Mayzing()
                client.gql = Mock(return_value={"cloneGroup": value})
                with self.assertRaises(mayzing.MayzingError):
                    client.clone_group("origin", ["image-1"])
                client.gql.assert_called_once()


class FakeMayzing:
    def __init__(self):
        self.campaigns = [{"id": "origin", "slug": "origin", "name": "ORIGIN", "clonable": True,
            "productIds": ['origin-shirt', 'origin-hoodie'], 'heroProductId': 'origin-shirt',
            'products': [{'id': 'origin-shirt', 'baseProductId': 'shirt'},
                         {'id': 'origin-hoodie', 'baseProductId': 'hoodie'}]}]
        self.images = {}
        self.clone_calls = 0
        self.waited = []
        self.lose_response = False
        self.hide_new = False
        self.reject_update = False
        self.reject_cover = False
        self.cover_updates = []
        self.reject_task_reads = False
        self.clone_processing = False
        self.incomplete_products = False
        self.updates = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def stores(self):
        return [{"slug": "TEST", "domainName": "shop.test"}]

    def groups(self, store):
        return [dict(g) for g in self.campaigns if not self.hide_new or g["id"] == "origin"]

    def group(self, store, group_id):
        return next(dict(g) for g in self.campaigns if g["id"] == group_id)

    def group_products(self, store, group_id):
        return self.group(store, group_id)

    def set_first_product(self, store, group, product_id):
        if self.reject_cover:
            raise mayzing.MayzingError('cover update failed')
        current = next(g for g in self.campaigns if g['id'] == group['id'])
        current['productIds'] = [product_id] + [p for p in current['productIds'] if p != product_id]
        current['heroProductId'] = product_id
        self.cover_updates.append((group['id'], product_id))
        return dict(current)

    def slug_free(self, store, slug):
        return all(g["slug"] != slug for g in self.campaigns)

    def upload_image(self, path, name):
        image_id = f"image-{len(self.images)}"
        self.images[image_id] = mayzing._safe_filename(name)
        return {"id": image_id, "name": self.images[image_id]}

    def clone_group(self, origin_id, image_ids):
        # Check that the disk journal exists before the remote mutation starts.
        self.clone_calls += 1
        rows = manifest.read("batch")
        assert all(json.loads(r["clone_state"])["before_ids"] == ["origin"] for r in rows)
        tasks = []
        for image_id in image_ids:
            group_id = f"group-{len(self.campaigns)}"
            self.campaigns.append({"id": group_id, "slug": group_id, "name": self.images[image_id], "tags": [],
                'processing': self.clone_processing,
                'productIds': [group_id+'-shirt', group_id+'-hoodie'], 'heroProductId': group_id+'-shirt',
                'products': [{'id': group_id+'-shirt', 'baseProductId': 'shirt'},
                             {'id': group_id+'-hoodie', 'baseProductId': 'hoodie'}]})
            if self.incomplete_products:
                self.campaigns[-1]['productIds'].pop()
            tasks.append({"id": f"task-{group_id}"})
        if self.lose_response:
            raise mayzing.SessionExpired("Response lost after clone")
        return tasks

    def wait_task(self, task_id, on_progress=None):
        self.waited.append(task_id)
        if self.reject_task_reads:
            raise mayzing.MayzingError("Internal Server Error")
        return {"id": task_id, "status": "SUCCEEDED", "summary": {"succeeded": 1}}

    def update_group(self, store, group_id, name, slug, tags):
        if self.reject_update:
            raise mayzing.MayzingError("update failed")
        group = next(g for g in self.campaigns if g["id"] == group_id)
        group.update(name=name, slug=slug, tags=tags)
        self.updates.append(group_id)
        return dict(group)


class UploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mayzing-upload-test-")))
        self.stack.enter_context(patch.object(config, "OUTPUT_DIR", root / "output"))
        self.stack.enter_context(patch.object(config, "LOG_DIR", root / "logs"))
        self.settings = {"store_slug": "TEST"}
        self.stack.enter_context(patch.object(config, "load_settings", return_value=self.settings))
        self.fake = FakeMayzing()
        self.stack.enter_context(patch.object(mayzing, "Mayzing", return_value=self.fake))
        self.rows = [{"filename": f"{n}.png", "title": f"TITLE {n}", "slug": f"url-{n}",
                      "origin_id": "origin", "status": "pending"} for n in range(2)]
        manifest.write("batch", self.rows)
        for row in self.rows:
            (config.OUTPUT_DIR / "batch" / row["filename"]).write_bytes(b"fake artwork")

    def run_batch(self, **kwargs):
        return upload_batch.run("batch", publish=False, **kwargs)

    def test_waits_for_every_task(self):
        result = self.run_batch()
        self.assertEqual(result["done"], 2)
        self.assertEqual(self.fake.waited, ["task-group-1", "task-group-2"])
        self.assertEqual(self.fake.clone_calls, 1)
        self.assertTrue(all(manifest.is_done(r) for r in manifest.read("batch")))

    def test_task_wait_reports_missing_and_unchanged_progress_without_flooding(self):
        clock = {"now": 0}
        messages = []

        def wait_task(task_id, on_progress=None):
            self.fake.waited.append(task_id)
            started = clock["now"]
            for elapsed, status, percent in ((0, "PENDING", None), (8, "PENDING", None),
                    (30, "PENDING", None), (34, "RUNNING", None), (40, "RUNNING", None),
                    (60, "RUNNING", 0), (64, "RUNNING", 0), (94, "RUNNING", 0)):
                clock["now"] = started + elapsed
                on_progress({"id": task_id, "status": status, "summary": {"progress": percent}})
            return {"id": task_id, "status": "SUCCEEDED", "summary": {"succeeded": 1}}

        self.fake.wait_task = wait_task
        with patch.object(upload_batch.time, "monotonic", side_effect=lambda: clock["now"]):
            result = self.run_batch(say=messages.append)
        self.assertEqual(result["done"], 2)
        self.assertEqual(self.fake.clone_calls, 1)
        for task_id in self.fake.waited:
            updates = [m for m in messages if f"task {task_id}:" in m and "đã chờ" in m]
            self.assertEqual(len(updates), 5)
            self.assertTrue(any("PENDING (đã chờ 0 giây)" in m for m in updates))
            self.assertTrue(any("PENDING (đã chờ 30 giây)" in m for m in updates))
            self.assertTrue(any("RUNNING (đã chờ 34 giây)" in m for m in updates))
            self.assertTrue(any("RUNNING 0% (đã chờ 94 giây)" in m for m in updates))

    def test_status_query_error_reconciles_all_existing_campaigns(self):
        self.fake.reject_task_reads = True
        messages = []
        result = self.run_batch(say=messages.append)
        self.assertEqual(result["done"], 2)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(self.fake.waited, ["task-group-1", "task-group-2"])
        self.assertEqual(self.fake.clone_calls, 1)
        self.assertTrue(any("không đọc được trạng thái task" in msg for msg in messages))
        self.assertFalse(any("Bulk clone: Internal Server Error" in msg for msg in messages))
        for row in manifest.read("batch"):
            state = json.loads(row["clone_state"])
            self.assertEqual(state["task_ids"], ["task-group-1", "task-group-2"])
            self.assertTrue(state["task_status_unavailable"])
            self.assertEqual(state["expected_product_count"], 2)

    def test_task_query_error_with_missing_groups_never_reclones(self):
        self.fake.reject_task_reads = True
        self.fake.hide_new = True
        with patch.object(upload_batch.time, 'monotonic', side_effect=[0, 0, 0, 61]):
            result = self.run_batch()
        self.assertEqual(result['errors'], 2)
        self.assertFalse(self.fake.updates)
        self.assertEqual(self.run_batch(retry_errors=True)['errors'], 2)
        self.assertEqual(self.fake.clone_calls, 1)

    def test_campaign_discovery_reports_wait_without_submitting_clone_again(self):
        clock = {"now": 0}
        messages = []
        groups = self.fake.groups

        def delayed_groups(store):
            found = groups(store)
            if self.fake.clone_calls and clock["now"] < 40:
                return [g for g in found if g["id"] == "origin"]
            return found

        def sleep(seconds):
            clock["now"] += seconds

        self.fake.groups = delayed_groups
        with patch.object(upload_batch.time, "monotonic", side_effect=lambda: clock["now"]), \
                patch.object(upload_batch.time, "sleep", side_effect=sleep):
            result = self.run_batch(say=messages.append)
        self.assertEqual(result["done"], 2)
        self.assertEqual(self.fake.clone_calls, 1)
        self.assertTrue(any("chờ campaign mới: tìm thấy 0/2" in m for m in messages))

    def test_task_query_error_waits_for_campaign_processing_before_updates(self):
        self.fake.reject_task_reads = True
        self.fake.clone_processing = True
        with patch.object(config, 'GROUP_READY_TIMEOUT_S', 0):
            self.assertEqual(self.run_batch()['errors'], 2)
        self.assertFalse(self.fake.updates)
        for group in self.fake.campaigns:
            group['processing'] = False
        self.assertEqual(self.run_batch(retry_errors=True)['done'], 2)
        self.assertEqual(self.fake.clone_calls, 1)

    def test_task_query_error_does_not_finish_incomplete_products_on_resume(self):
        self.fake.reject_task_reads = True
        self.fake.incomplete_products = True
        self.assertEqual(self.run_batch()['errors'], 2)
        self.assertFalse(self.fake.updates)
        self.assertEqual(self.run_batch(retry_errors=True)['errors'], 2)
        self.assertFalse(self.fake.updates)
        self.assertEqual(self.fake.clone_calls, 1)

    def test_old_task_journal_requires_complete_products_when_resuming(self):
        self.fake.lose_response = True
        self.fake.incomplete_products = True
        with self.assertRaises(mayzing.SessionExpired):
            self.run_batch()
        rows = manifest.read('batch')
        for row in rows:
            state = json.loads(row['clone_state'])
            state.pop('expected_product_count')
            state['task_ids'] = ['task-group-1', 'task-group-2']
            row['clone_state'] = json.dumps(state)
        manifest.write('batch', rows)
        self.assertEqual(self.run_batch(retry_errors=True)['errors'], 2)
        self.assertFalse(self.fake.updates)
        self.assertEqual(self.fake.clone_calls, 1)
        self.assertTrue(all(json.loads(row['clone_state'])['require_complete_products']
                            for row in manifest.read('batch')))
        for group in self.fake.campaigns[1:]:
            group['productIds'].append(group['id']+'-hoodie')
        self.assertEqual(self.run_batch(retry_errors=True)['done'], 2)
        self.assertEqual(self.fake.clone_calls, 1)

    def test_lost_clone_reply_recovers_existing_campaigns(self):
        self.fake.lose_response = True
        with self.assertRaises(mayzing.SessionExpired):
            self.run_batch()
        result = self.run_batch()
        self.assertEqual(result["done"], 2)
        self.assertEqual(self.fake.clone_calls, 1)
        self.assertEqual(len(self.fake.images), 2)

    def test_missing_campaign_after_submitted_clone_blocks_resubmission(self):
        self.fake.lose_response = True
        with self.assertRaises(mayzing.SessionExpired):
            self.run_batch()
        self.fake.hide_new = True
        result = self.run_batch()
        self.assertEqual(result["errors"], 2)
        self.assertEqual(self.fake.clone_calls, 1)

    def test_failed_rename_resumes_by_group_id(self):
        self.fake.reject_update = True
        self.assertEqual(self.run_batch()["errors"], 2)
        self.fake.reject_update = False
        # Recovery needs no local print files and creates no new artwork.
        for path in (config.OUTPUT_DIR / "batch").glob("*.png"):
            path.unlink()
        self.assertEqual(self.run_batch(retry_errors=True)["done"], 2)
        self.assertEqual(self.fake.clone_calls, 1)
        self.assertEqual(len(self.fake.images), 2)

    def test_preferred_product_uses_cloned_ids(self):
        self.settings['default_products'] = {'origin': {'base_product_id': 'hoodie'}}
        self.assertEqual(self.run_batch()['done'], 2)
        self.assertEqual(self.fake.cover_updates, [('group-1', 'group-1-hoodie'), ('group-2', 'group-2-hoodie')])
        self.assertTrue(all(g['heroProductId'].endswith('-hoodie') for g in self.fake.campaigns[1:]))

    def test_failed_cover_update_resumes_without_cloning_again(self):
        self.settings['default_products'] = {'origin': {'base_product_id': 'hoodie'}}
        self.fake.reject_cover = True
        self.assertEqual(self.run_batch()['errors'], 2)
        self.fake.reject_cover = False
        self.assertEqual(self.run_batch(retry_errors=True)['done'], 2)
        self.assertEqual(self.fake.clone_calls, 1)
        self.assertEqual(len(self.fake.cover_updates), 2)

    def test_missing_preferred_type_blocks_before_upload(self):
        self.settings['default_products'] = {'origin': {'base_product_id': 'missing'}}
        self.assertEqual(self.run_batch()['errors'], 2)
        self.assertEqual(self.fake.clone_calls, 0)
        self.assertFalse(self.fake.images)


class CampaignWaitTests(unittest.TestCase):
    def test_readiness_reports_ongoing_processing_every_30_seconds(self):
        clock = {"now": 0}
        messages = []
        group = {"id": "group-1", "slug": "new-campaign", "processing": True}
        ready = {**group, "processing": False}
        client = Mock()
        client.group.side_effect = [group] * 6 + [ready]

        def sleep(seconds):
            clock["now"] += seconds

        with patch.object(upload_batch.time, "monotonic", side_effect=lambda: clock["now"]), \
                patch.object(upload_batch.time, "sleep", side_effect=sleep), \
                patch.object(config, "CLONE_POLL_S", 10):
            result = upload_batch._wait_ready(client, "TEST", group, messages.append)
        self.assertIs(result, ready)
        self.assertEqual(client.group.call_count, 7)
        self.assertEqual(len(messages), 3)
        self.assertIn("chờ Mayzing xử lý sản phẩm", messages[0])
        self.assertIn("đã chờ 30 giây", messages[1])
        self.assertIn("đã chờ 60 giây", messages[2])
        self.assertEqual({call[0] for call in client.method_calls}, {"group"})

    def test_readiness_timeout_uses_monotonic_clock_and_preserves_stop(self):
        clock = {"now": 0}
        messages = []
        group = {"id": "group-1", "slug": "new-campaign", "processing": True}
        client = Mock()
        client.group.return_value = group

        def sleep(seconds):
            clock["now"] += seconds

        with patch.object(upload_batch.time, "monotonic", side_effect=lambda: clock["now"]), \
                patch.object(upload_batch.time, "time", side_effect=AssertionError("wall clock must not control waits")), \
                patch.object(upload_batch.time, "sleep", side_effect=sleep), \
                patch.object(config, "GROUP_READY_TIMEOUT_S", 60), \
                patch.object(config, "CLONE_POLL_S", 10):
            with self.assertRaisesRegex(mayzing.MayzingError, "tool sẽ không clone lại"):
                upload_batch._wait_ready(client, "TEST", group, messages.append)
        self.assertEqual(client.group.call_count, 6)
        self.assertTrue(any("đã chờ 30 giây" in m for m in messages))
        self.assertEqual({call[0] for call in client.method_calls}, {"group"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
