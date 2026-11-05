"""Start the real WSGI server with disposable data and check its health."""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch

import supervisor

ROOT = Path(__file__).resolve().parents[1]


def smoke(release: str, timeout: float = 30) -> None:
    with tempfile.TemporaryDirectory(prefix="mayzing-smoke-") as tmp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        sha = release if supervisor.SHA.fullmatch(release) else hashlib.sha256(release.encode()).hexdigest()[:40]
        root = Path(tmp)
        prepared = root / "releases" / sha
        prepared.mkdir(parents=True)
        (root / "logs").mkdir()
        (prepared / ".prepared").write_text(sha, encoding="ascii")
        # Exercise the actual Supervisor.start/stop process ownership code with
        # this interpreter's already-installed dependencies. Only release-path
        # lookup is substituted; the WSGI server and Windows job are real.
        (prepared / "production.py").write_text(
            f"import runpy, sys\nsys.path.insert(0, {str(ROOT)!r})\n"
            f"runpy.run_path({str(ROOT / 'production.py')!r}, run_name='__main__')\n", encoding="utf-8")
        supervisor.write_json(root / "state.json", {"current": sha})
        settings = {"data_root": str(root / "data"), "port": port}
        managed = supervisor.Supervisor(root, settings)
        try:
            with patch.object(supervisor, "python_for", return_value=Path(sys.executable)):
                managed.start(sha)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as res:
                data = json.load(res)
                assert res.status == 200 and data.get("ok") is True, data
                assert data.get("release") == sha, data
                assert data.get("active_jobs") == 0 and data.get("active_requests") == 0, data
                assert "Set-Cookie" not in res.headers, "Health must not allocate a workspace."
            print(f"Production supervisor smoke passed ({release}).")
        except Exception as exc:
            path = root / "logs" / f"server-{sha}.log"
            log = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            raise RuntimeError("Production supervisor smoke failed:\n" + log) from exc
        finally:
            managed.stop()
            managed.job.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="smoke-test")
    args = parser.parse_args()
    smoke(args.release)
