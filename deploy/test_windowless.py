"""Exercise the real windowless Windows controller with disposable application data."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import supervisor

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windowless scheduler entry point is Windows-specific")
class WindowlessTests(unittest.TestCase):
    def test_repeated_release_switches_keep_process_ownership(self):
        with tempfile.TemporaryDirectory(prefix="mayzing-restart-") as folder:
            root = Path(folder)
            (root / "logs").mkdir()
            releases = ("a" * 40, "b" * 40)
            for sha in releases:
                release = root / "releases" / sha
                release.mkdir(parents=True)
                (release / ".prepared").write_text(sha, encoding="ascii")
                (release / "production.py").write_text(
                    f"import runpy, sys\nsys.path.insert(0, {str(ROOT)!r})\n"
                    f"runpy.run_path({str(ROOT / 'production.py')!r}, run_name='__main__')\n", encoding="utf-8")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            supervisor.write_json(root / "state.json", {"current": releases[0]})
            managed = supervisor.Supervisor(root, {"data_root": str(root / "data"), "port": port})
            try:
                with patch.object(supervisor, "python_for", return_value=Path(sys.executable)):
                    for number in range(8):
                        sha = releases[number % 2]
                        previous_job = managed.job
                        managed.start(sha)
                        self.assertIsNot(managed.job, previous_job)
                        self.assertIsNone(previous_job.handle)
                        self.assertEqual(supervisor.health(port)["release"], sha)
                        managed.stop()
                        # A venv redirector may exit first; stop still waits for
                        # the complete owned WSGI tree and releases its port.
                        self.assertIsNone(managed.proc)
                        with self.assertRaises((urllib.error.URLError, TimeoutError, ConnectionError)):
                            supervisor.health(port, timeout=1)
            finally:
                managed.stop()
                managed.job.close()

    def test_windowless_controller_serves_and_writes_lifecycle_log(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        self.assertTrue(pythonw.is_file())
        with tempfile.TemporaryDirectory(prefix="mayzing-windowless-") as folder:
            root = Path(folder)
            sha = "a" * 40
            release = root / "releases" / sha
            release.mkdir(parents=True)
            (root / "paused").touch()
            (release / ".prepared").write_text(sha, encoding="ascii")
            (release / "production.py").write_text(
                f"import runpy, sys\nsys.path.insert(0, {str(ROOT)!r})\n"
                f"runpy.run_path({str(ROOT / 'production.py')!r}, run_name='__main__')\n", encoding="utf-8")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            supervisor.write_json(root / "settings.json", {
                "repository": "owner/repo", "branch": "main", "data_root": str(root / "data"), "port": port})
            supervisor.write_json(root / "state.json", {"current": sha})
            # Substitute only the already-installed interpreter. The windowless
            # controller, file logging, process ownership and real WSGI are exercised.
            bootstrap = root / "launch.py"
            bootstrap.write_text(
                f"import sys\nfrom pathlib import Path\nsys.path.insert(0, {str(ROOT / 'deploy')!r})\n"
                f"import supervisor\nsupervisor.python_for = lambda release: Path({sys.executable!r})\n"
                f"sys.argv = ['supervisor', '--root', {str(root)!r}]\nsupervisor.main()\n", encoding="utf-8")
            job = supervisor.WindowsChildJob()
            proc = job.spawn([str(pythonw), str(bootstrap)])
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                            health = json.load(response)
                        self.assertTrue(health["ok"])
                        self.assertEqual(health["release"], sha)
                        text = (root / "logs" / "supervisor.log").read_text(encoding="utf-8")
                        if "Serving release" not in text:
                            time.sleep(0.1)
                            continue
                        self.assertIn("Supervisor started pid=", text)
                        self.assertIn("Serving release", text)
                        break
                    except (urllib.error.URLError, TimeoutError, ConnectionError):
                        time.sleep(0.2)
                else:
                    self.fail("Windowless controller did not serve health before timeout")
            finally:
                job.terminate()
                proc.wait(timeout=15)
                job.close()


if __name__ == "__main__":
    unittest.main()
