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


class ServerLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(supervisor.os.name == "nt", "Windows error codes are platform-specific")
    def test_failed_assignment_keeps_original_error_after_process_cleanup(self):
        job = supervisor.WindowsChildJob.__new__(supervisor.WindowsChildJob)
        job.handle = 77
        job.kernel = Mock()
        job.kernel.AssignProcessToJobObject.return_value = 0
        proc = Mock(_handle=42, pid=123)
        proc.poll.return_value = None
        proc.terminate.side_effect = lambda: supervisor.ctypes.set_last_error(87)
        with patch.object(supervisor.subprocess, "Popen", return_value=proc):
            supervisor.ctypes.set_last_error(5)
            with self.assertRaises(OSError) as failure:
                job.spawn(["disposable-test-process"])
        self.assertEqual(failure.exception.winerror, 5)
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=15)

    def create_root(self, root):
        (root / "logs").mkdir()
        for sha in (SHA1, SHA2):
            release = root / "releases" / sha
            release.mkdir(parents=True)
            (release / ".prepared").write_text(sha, encoding="ascii")
        supervisor.write_json(root / "state.json", {"current": SHA1})
        return {"data_root": str(root / "data"), "port": 8780}

    def job(self):
        job = Mock()
        job.spawn.return_value.poll.return_value = None
        return job

    def test_every_release_and_rollback_uses_a_fresh_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.create_root(root)
            initial, first, replacement, rollback = [self.job() for _ in range(4)]
            with patch.object(supervisor, "WindowsChildJob", side_effect=[initial, first, replacement, rollback]), \
                    patch.object(supervisor, "health", side_effect=[{"release": SHA1}, {"release": SHA2}, {"release": SHA1}]):
                instance = supervisor.Supervisor(root, settings)
                for sha, expected in ((SHA1, first), (SHA2, replacement), (SHA1, rollback)):
                    instance.start(sha)
                    self.assertIs(instance.job, expected)
                    expected.spawn.assert_called_once()
                    instance.stop()
                instance.job.close()
            initial.close.assert_called_once()
            first.close.assert_called_once()
            replacement.close.assert_called_once()
            rollback.close.assert_called_once()

    def test_assignment_failure_closes_handles_and_rollback_uses_another_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.create_root(root)
            initial, rejected, rollback = [self.job() for _ in range(3)]
            rejected.spawn.side_effect = PermissionError(5, "Assignment failed")
            with patch.object(supervisor, "WindowsChildJob", side_effect=[initial, rejected, rollback]), \
                    patch.object(supervisor, "health", return_value={"release": SHA1}):
                instance = supervisor.Supervisor(root, settings)
                with self.assertRaisesRegex(PermissionError, "Assignment failed"):
                    instance.start(SHA2)
                self.assertIsNone(instance.proc)
                self.assertIsNone(instance.log)
                self.assertTrue(rejected.spawn.call_args.kwargs["stdout"].closed)
                rejected.close.assert_called_once()
                instance.start(SHA1)
                self.assertIs(instance.job, rollback)
                instance.stop()
                instance.job.close()

    def test_start_never_closes_a_job_that_still_owns_a_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.create_root(root)
            job = self.job()
            with patch.object(supervisor, "WindowsChildJob", return_value=job):
                instance = supervisor.Supervisor(root, settings)
                instance.proc = Mock()
                with self.assertRaisesRegex(RuntimeError, "Stop the owned server"):
                    instance.start(SHA2)
            job.close.assert_not_called()
            job.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
