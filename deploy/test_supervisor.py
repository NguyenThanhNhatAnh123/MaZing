"""Offline regression tests for deployment gates, archives, and rollback."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import supervisor

SHA1 = "a" * 40
SHA2 = "b" * 40
REPO = "owner/repo"


def run(**changes):
    result = {"id": 1, "run_attempt": 1, "head_sha": SHA1, "head_branch": "main", "event": "push",
              "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success",
              "head_repository": {"full_name": REPO}}
    result.update(changes)
    return result


class ApprovalTests(unittest.TestCase):
    def test_successful_main_push_only(self):
        self.assertTrue(supervisor.approved_run([run()], REPO, "main", SHA1))
        for change in ({"event": "pull_request"}, {"head_branch": "feature"}, {"head_sha": SHA2},
                       {"head_repository": {"full_name": "attacker/repo"}}, {"path": ".github/workflows/other.yml"},
                       {"conclusion": "failure"}, {"status": "in_progress"}):
            self.assertFalse(supervisor.approved_run([run(**change)], REPO, "main", SHA1), change)

    def test_newer_failure_or_pending_rerun_blocks_old_success(self):
        self.assertFalse(supervisor.approved_run([run(), run(id=2, conclusion="failure")], REPO, "main", SHA1))
        self.assertFalse(supervisor.approved_run([run(), run(run_attempt=2, status="queued")], REPO, "main", SHA1))

    def test_default_branch_head_must_match_bootstrap(self):
        with patch.object(supervisor, "request_json", side_effect=[{"default_branch": "main"}, {"object": {"sha": SHA2}}]):
            with self.assertRaisesRegex(RuntimeError, "no longer"):
                supervisor.candidate(REPO, "main", SHA1)


class ArchiveTests(unittest.TestCase):
    def archive(self, name: str, symlink=False) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("repo-commit/", "")
            info = zipfile.ZipInfo(name)
            if symlink:
                info.external_attr = 0o120777 << 16
            z.writestr(info, "safe")
        return buffer.getvalue()

    def test_safe_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supervisor.extract_release(self.archive("repo-commit/app/main.py"), root)
            self.assertEqual((root / "app" / "main.py").read_text(), "safe")

    def test_unsafe_archive(self):
        for name, symlink in (("repo-commit/../escaped", False), ("different/file", False),
                              ("repo-commit/C:/outside", False), ("repo-commit/..\\escaped", False),
                              ("repo-commit/link", True)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    supervisor.extract_release(self.archive(name, symlink), Path(tmp))


class RollbackTests(unittest.TestCase):
    def instance(self, root: Path):
        instance = supervisor.Supervisor.__new__(supervisor.Supervisor)
        instance.root = root
        instance.settings = {"port": 8780, "drain_timeout": 1, "repository": REPO, "branch": "main"}
        instance.state_file = root / "state.json"
        instance.state = {"current": SHA1}
        instance.drain = root / "draining"
        instance.start = Mock()
        instance.stop = Mock()
        return instance

    def test_failed_health_rolls_back_and_suppresses_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self.instance(Path(tmp))
            instance.start.side_effect = [RuntimeError("unhealthy"), None]
            with patch.object(supervisor, "prepare"), patch.object(supervisor, "candidate", return_value=SHA2), \
                    patch.object(supervisor, "health", return_value={"active_jobs": 0, "active_requests": 0}):
                self.assertFalse(instance.deploy(SHA2))
            self.assertEqual(instance.start.call_args_list[0].args, (SHA2,))
            self.assertEqual(instance.start.call_args_list[1].args, (SHA1,))
            state = json.loads(instance.state_file.read_text())
            self.assertEqual(state["current"], SHA1)
            self.assertEqual(state["failed"], SHA2)
            self.assertFalse(instance.drain.exists())

    def test_busy_jobs_postpone_without_terminating_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self.instance(Path(tmp))
            with patch.object(supervisor, "prepare"), patch.object(supervisor, "candidate", return_value=SHA2), \
                    patch.object(supervisor, "health", return_value={"active_jobs": 1, "active_requests": 0}), \
                    patch.object(supervisor.time, "monotonic", side_effect=[0, 0, 2]), patch.object(supervisor.STOP, "wait", return_value=False):
                self.assertFalse(instance.deploy(SHA2))
            instance.stop.assert_not_called()
            instance.start.assert_not_called()
            self.assertEqual(instance.state["current"], SHA1)
            self.assertFalse(instance.drain.exists())

    def test_success_persists_new_and_previous_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self.instance(Path(tmp))
            with patch.object(supervisor, "prepare"), patch.object(supervisor, "candidate", return_value=SHA2), \
                    patch.object(supervisor, "health", return_value={"active_jobs": 0, "active_requests": 0}):
                self.assertTrue(instance.deploy(SHA2))
            state = json.loads(instance.state_file.read_text())
            self.assertEqual((state["current"], state["previous"]), (SHA2, SHA1))
            self.assertFalse(instance.drain.exists())

    def test_superseded_release_never_drains_or_stops_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self.instance(Path(tmp))
            with patch.object(supervisor, "prepare"), patch.object(supervisor, "candidate", return_value="c" * 40):
                self.assertFalse(instance.deploy(SHA2))
            instance.stop.assert_not_called()
            instance.start.assert_not_called()
            self.assertFalse(instance.drain.exists())

    def test_inflight_synchronous_upload_postpones_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self.instance(Path(tmp))
            with patch.object(supervisor, "prepare"), patch.object(supervisor, "candidate", return_value=SHA2), \
                    patch.object(supervisor, "health", return_value={"active_jobs": 0, "active_requests": 1}), \
                    patch.object(supervisor.time, "monotonic", side_effect=[0, 0, 2]), \
                    patch.object(supervisor.STOP, "wait", return_value=False):
                self.assertFalse(instance.deploy(SHA2))
            instance.stop.assert_not_called()
            self.assertFalse(instance.drain.exists())


class MonitoringTests(unittest.TestCase):
    def test_server_recovery_does_not_wait_for_github_poll_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = RollbackTests().instance(Path(tmp))
            instance.proc = Mock()
            instance.proc.poll.side_effect = [None, 1, None]
            instance.job = Mock()
            with patch.object(supervisor, "candidate", return_value=SHA1) as poll, \
                    patch.object(supervisor.time, "monotonic", side_effect=[0, 5, 10]), \
                    patch.object(supervisor.STOP, "is_set", side_effect=[False, False, False, True]), \
                    patch.object(supervisor.STOP, "wait", return_value=False) as wait:
                instance.run()
            self.assertEqual(instance.start.call_count, 2)
            self.assertEqual(poll.call_count, 1)
            self.assertEqual([call.args[0] for call in wait.call_args_list], [5, 5, 5])
            instance.job.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
