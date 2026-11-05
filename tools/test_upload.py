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
                'productIds': [group_id+'-shirt', group_id+'-hoodie'], 'heroProductId': group_id+'-shirt',
                'products': [{'id': group_id+'-shirt', 'baseProductId': 'shirt'},
                             {'id': group_id+'-hoodie', 'baseProductId': 'hoodie'}]})
            tasks.append({"id": f"task-{group_id}"})
        if self.lose_response:
            raise mayzing.SessionExpired("Response lost after clone")
        return tasks

    def wait_task(self, task_id, on_progress=None):
        self.waited.append(task_id)
        return {"id": task_id, "status": "SUCCEEDED", "summary": {"succeeded": 1}}

    def update_group(self, store, group_id, name, slug, tags):
        if self.reject_update:
            raise mayzing.MayzingError("update failed")
        group = next(g for g in self.campaigns if g["id"] == group_id)
        group.update(name=name, slug=slug, tags=tags)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
