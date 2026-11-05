"""Private deployment agent: run only CI-approved main commits behind the tunnel.

This process owns its WSGI child. It never kills processes found by name or PID,
never modifies the working checkout, and never executes pull-request code.
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
STOP = threading.Event()


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def request_json(url: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "MaZing-deploy/1.0",
               "X-GitHub-Api-Version": "2022-11-28", "Cache-Control": "no-cache"}
    # A public repository needs no token. Optional token is read from this host's
    # environment and is never written to configuration, releases, or logs.
    token = os.environ.get("MAYZING_DEPLOY_GITHUB_TOKEN", "")
    if token and urllib.parse.urlsplit(url).netloc == "api.github.com":
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def approved_run(runs: list[dict], repository: str, branch: str, sha: str) -> bool:
    matching = [run for run in runs if run.get("head_sha") == sha
                and run.get("head_branch") == branch
                and run.get("event") == "push"
                and run.get("path") == ".github/workflows/ci.yml"
                and (run.get("head_repository") or {}).get("full_name", "").lower() == repository.lower()]
    if not matching:
        return False
    latest = max(matching, key=lambda run: (int(run.get("id", 0)), int(run.get("run_attempt", 0))))
    return latest.get("status") == "completed" and latest.get("conclusion") == "success"


def candidate(repository: str, branch: str, expected: str | None = None) -> str | None:
    base = "https://api.github.com/repos/" + repository
    metadata = request_json(base)
    if metadata.get("default_branch") != branch:
        raise RuntimeError("Deployment branch must be the repository's default branch.")
    head = request_json(base + "/git/ref/heads/" + urllib.parse.quote(branch, safe=""))["object"]["sha"]
    if not SHA.fullmatch(head):
        raise RuntimeError("GitHub returned an invalid commit SHA.")
    if expected is not None and head != expected:
        raise RuntimeError("Bootstrap commit is no longer the default branch head.")
    query = urllib.parse.urlencode({"branch": branch, "event": "push", "head_sha": head, "per_page": 20})
    runs = request_json(base + "/actions/workflows/ci.yml/runs?" + query)
    return head if approved_run(runs.get("workflow_runs", []), repository, branch, head) else None


def extract_release(blob: bytes, destination: Path) -> None:
    """Reject traversal, symlinks and unexpectedly large source archives."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = archive.infolist()
        if not members or sum(item.file_size for item in members) > 250 * 1024 * 1024:
            raise ValueError("Empty or oversized source archive.")
        prefix = PurePosixPath(members[0].filename).parts[0]
        for member in members:
            parts = PurePosixPath(member.filename).parts
            if not parts or parts[0] != prefix or any(p in ("..", ".") for p in parts):
                raise ValueError("Unsafe archive path.")
            relative = Path(*parts[1:])
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()) or "\\" in member.filename or ":" in member.filename:
                raise ValueError("Archive path escapes the release directory.")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Symbolic links are not supported in releases.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif relative.parts:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    while block := source.read(1024 * 1024):
                        output.write(block)


def python_for(release: Path) -> Path:
    return release / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare(root: Path, settings: dict, sha: str) -> Path:
    if not SHA.fullmatch(sha):
        raise ValueError("A full immutable commit SHA is required.")
    release = root / "releases" / sha
    if (release / ".prepared").is_file() and python_for(release).is_file():
        return release
    if release.exists():
        # Preserve incomplete releases for diagnosis. Never recurse-delete state.
        release.rename(release.with_name(sha + "-incomplete-" + uuid.uuid4().hex[:8]))
    release.mkdir(parents=True)
    url = f"https://codeload.github.com/{settings['repository']}/zip/{sha}"
    with urllib.request.urlopen(url, timeout=120) as response:
        blob = response.read(100 * 1024 * 1024 + 1)
    if len(blob) > 100 * 1024 * 1024:
        raise ValueError("Source archive is too large.")
    extract_release(blob, release)
    if not (release / "production.py").is_file() or not (release / "requirements.txt").is_file():
        raise RuntimeError("Commit does not contain the production entry point.")
    env = dict(os.environ, PYTHONUTF8="1")
    with (root / "logs" / f"prepare-{sha}.log").open("a", encoding="utf-8") as log:
        def run(args: list[str], timeout: int) -> None:
            subprocess.run(args, cwd=release, env=env, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=timeout)
        run([settings["python"], "-m", "venv", str(release / ".venv")], 120)
        run([str(python_for(release)), "-m", "pip", "install", "--disable-pip-version-check",
             "-r", str(release / "requirements.txt")], 900)
        run([str(python_for(release)), str(release / "deploy" / "smoke.py"), "--release", sha], 60)
    (release / ".prepared").write_text(sha + "\n", encoding="ascii")
    return release


def health(port: int, timeout: float = 3) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health", headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.load(response)
        if response.status != 200 or data.get("ok") is not True:
            raise RuntimeError("Unhealthy WSGI server.")
        for name in ("active_jobs", "active_requests"):
            if not isinstance(data.get(name), int) or data[name] < 0:
                raise RuntimeError(f"Health response is missing its {name} count.")
        return data


class WindowsChildJob:
    """Close the managed child if Task Scheduler terminates this supervisor."""
    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.DWORD, ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def attach(self, proc: subprocess.Popen) -> None:
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(proc._handle)):
            # Capture the assignment error before any cleanup can overwrite it.
            raise ctypes.WinError(ctypes.get_last_error())

    def spawn(self, args: list[str], **kwargs) -> subprocess.Popen:
        if not self.handle:
            return subprocess.Popen(args, **kwargs)
        # A Windows venv python.exe is a redirector that launches another Python
        # process. Suspend it until job assignment so even the earliest child is
        # owned by the job. Attaching an already running redirector has a race.
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000004  # CREATE_SUSPENDED
        proc = subprocess.Popen(args, **kwargs)
        try:
            self.attach(proc)
            self._resume(proc.pid)
            return proc
        except Exception:
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=15)
            except Exception:
                logging.exception("Failed to stop rejected managed process pid=%s", proc.pid)
            raise

    def _resume(self, pid: int) -> None:
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("thread_id", wintypes.DWORD),
                        ("process_id", wintypes.DWORD), ("priority", wintypes.LONG),
                        ("delta_priority", wintypes.LONG), ("flags", wintypes.DWORD)]

        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenThread.restype = wintypes.HANDLE
        self.kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.process_id == pid:
                    thread = self.kernel.OpenThread(0x0002, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        self.kernel.CloseHandle(thread)
                available = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError("Suspended server thread was not found.")
        finally:
            self.kernel.CloseHandle(snapshot)

    def terminate(self) -> None:
        if not self.handle:
            return
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
                        ("period_user", ctypes.c_int64), ("period_kernel", ctypes.c_int64),
                        ("page_faults", wintypes.DWORD), ("processes", wintypes.DWORD),
                        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]

        end = time.monotonic() + 15
        while time.monotonic() < end:
            info = Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.active == 0:
                return
            time.sleep(0.05)
        raise RuntimeError("Managed process tree did not stop within 15 seconds.")

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Supervisor:
    def __init__(self, root: Path, settings: dict) -> None:
        self.root = root
        self.settings = settings
        self.state_file = root / "state.json"
        self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.drain = root / "draining"
        self.proc: subprocess.Popen | None = None
        self.log = None
        self.job = WindowsChildJob()

    def start(self, sha: str) -> None:
        release = self.root / "releases" / sha
        if not SHA.fullmatch(sha) or not (release / ".prepared").is_file():
            raise RuntimeError("Refusing to start an unprepared release.")
        if self.proc is not None:
            raise RuntimeError("Stop the owned server before starting another release.")
        # A terminated job may still be completing kernel teardown after its
        # active count reaches zero. Never assign the next release or rollback
        # to that old job; every server lifetime gets a fresh ownership boundary.
        self.job.close()
        self.job = WindowsChildJob()
        data_root = str(Path(self.settings["data_root"]).resolve())
        env = dict(os.environ, MAYZING_DATA_ROOT=data_root,
                   MAYZING_ENV_FILE=str(Path(data_root) / ".env"), MAYZING_RELEASE=sha,
                   MAYZING_DRAIN_FILE=str(self.drain), PYTHONUTF8="1")
        self.log = (self.root / "logs" / f"server-{sha}.log").open("a", encoding="utf-8")
        try:
            self.proc = self.job.spawn(
                [str(python_for(release)), str(release / "production.py"), "--port", str(self.settings["port"])],
                cwd=release, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            self.job.close()
            self.log.close()
            self.log = None
            raise
        end = time.monotonic() + 45
        while time.monotonic() < end and self.proc.poll() is None:
            try:
                if health(self.settings["port"]).get("release") == sha:
                    logging.info("Serving release %s", sha)
                    return
            except Exception:
                pass
            if STOP.wait(0.4):
                break
        self.stop()
        raise RuntimeError(f"Release {sha} did not become healthy; see server log.")

    def stop(self) -> None:
        if self.proc is not None:
            # Windows redirectors can exit before their actual Python child.
            # Always stop and wait for the complete job, even if the launcher's
            # Popen has already exited.
            if os.name == "nt":
                self.job.terminate()
            if self.proc.poll() is None:
                # This Popen belongs to this supervisor. A process discovered by
                # name, port, or a stale PID is never terminated here.
                if os.name != "nt":
                    self.proc.terminate()
                self.proc.wait(timeout=20)
            self.proc = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def deploy(self, sha: str) -> bool:
        prepare(self.root, self.settings, sha)
        if candidate(self.settings["repository"], self.settings["branch"]) != sha:
            logging.info("Prepared release was superseded or its CI approval changed; keeping current release.")
            return False
        old = self.state["current"]
        self.drain.write_text(sha + "\n", encoding="ascii")
        try:
            # The application rejects new mutations while bridge results and
            # heartbeats can finish. A busy job postpones deployment, never dies.
            end = time.monotonic() + self.settings.get("drain_timeout", 600)
            while time.monotonic() < end:
                status = health(self.settings["port"])
                if status["active_jobs"] == 0 and status["active_requests"] == 0:
                    break
                if STOP.wait(2):
                    return False
            else:
                logging.warning("Deployment deferred because jobs are still active.")
                return False
            self.stop()
            try:
                self.start(sha)
            except Exception:
                logging.exception("Release failed health check; rolling back to %s", old)
                self.stop()
                self.state["failed"] = sha
                write_json(self.state_file, self.state)
                self.start(old)
                return False
            self.state = {"current": sha, "previous": old, "failed": None,
                          "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            write_json(self.state_file, self.state)
            return True
        finally:
            self.drain.unlink(missing_ok=True)

    def run(self) -> None:
        # Recover a drain marker left by an interrupted supervisor. The next
        # deployment creates its own marker after a fresh health check.
        self.drain.unlink(missing_ok=True)
        logging.info("Supervisor started pid=%s release=%s", os.getpid(), self.state["current"])
        self.start(self.state["current"])
        next_check = 0.0
        try:
            while not STOP.is_set():
                try:
                    if self.proc is None or self.proc.poll() is not None:
                        logging.warning("Managed server stopped; restarting current release.")
                        self.stop()
                        self.start(self.state["current"])
                    now = time.monotonic()
                    if now >= next_check:
                        # Process recovery is independent of the slower GitHub polling
                        # interval and continues even when GitHub is unavailable.
                        next_check = now + max(300, self.settings.get("interval", 300))
                        sha = None if (self.root / "paused").exists() else candidate(
                            self.settings["repository"], self.settings["branch"])
                        if sha and sha not in (self.state.get("current"), self.state.get("failed")):
                            logging.info("Preparing CI-approved release %s", sha)
                            self.deploy(sha)
                except Exception:
                    logging.exception("Production supervision check failed; retaining current release.")
                STOP.wait(5)
        finally:
            logging.warning("Supervisor stopping pid=%s; closing managed server.", os.getpid())
            self.stop()
            self.job.close()
            self.drain.unlink(missing_ok=True)


def acquire_lock(path: Path):
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit("Another deployment supervisor is already running.")
    return handle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", metavar="SHA", help="Prepare initial CI-approved release without starting it")
    args = parser.parse_args()
    root = args.root.resolve()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(root / "logs" / "supervisor.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    if not REPOSITORY.fullmatch(settings["repository"]) or settings["branch"] != "main":
        raise SystemExit("Only a configured repository's default main branch is supported.")
    if not 1 <= int(settings["port"]) <= 65535:
        raise SystemExit("Invalid port.")
    lock = acquire_lock(root / "supervisor.lock")
    try:
        if args.prepare:
            existing = root / "state.json"
            if existing.exists() and json.loads(existing.read_text(encoding="utf-8"))["current"] != args.prepare:
                raise SystemExit("Already initialized. Let the supervisor perform updates.")
            if candidate(settings["repository"], settings["branch"], args.prepare) != args.prepare:
                raise SystemExit("Initial commit has no successful main push CI run yet.")
            prepare(root, settings, args.prepare)
            if not existing.exists():
                write_json(existing, {"current": args.prepare, "previous": None, "failed": None})
            logging.info("Initial release prepared; port %s has not been touched.", settings["port"])
            return
        def request_stop(signum, frame):
            logging.warning("Supervisor received stop signal %s", signum)
            STOP.set()

        for signum in {signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)}:
            signal.signal(signum, request_stop)
        Supervisor(root, settings).run()
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Deployment supervisor exited unexpectedly.")
        raise
