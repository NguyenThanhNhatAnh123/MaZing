"""Local web dashboard (http://127.0.0.1:8780/): designs -> prep -> review -> batch -> Mayzing.

One background job at a time (prep | upload | login); its log is shown on the page.
Run with run_dashboard.bat (or: python dashboard.py [--port N] [--no-browser]).
"""
from __future__ import annotations

import argparse
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import zipfile
from datetime import timedelta
from urllib.parse import urlsplit
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_file, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.local import LocalProxy

import config
from lib import manifest, mayzing, ocr, review, product_defaults, slug as slugs
from lib.browser_bridge import BRIDGE, DEFAULT_BRIDGE, BrowserBridge, BridgeError
from lib.context import BRIDGE_GENERATION, RUNTIME, WORKSPACE_ID, bind_context

config.ensure_dirs()
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config.update(SECRET_KEY=config.session_secret(), SESSION_COOKIE_NAME="mayzing_device",
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))


class DeviceSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, application):
        return request.host.lower() == config.PUBLIC_HOST


app.session_interface = DeviceSessionInterface()
log = logging.getLogger("dashboard")
_default_review = review.Review()
REVIEW = LocalProxy(lambda: RUNTIME.get().review if RUNTIME.get() is not None else _default_review)
PAGE = Path(__file__).resolve().parent / "lib" / "dashboard.html"
GUIDE_PAGE = config.ROOT / "lib" / "huongdan.html"
GUIDE_VIDEO = config.ROOT / "assets" / "huong-dan-cai-extension-developer-mode.mp4"
BTHUMB_DIR = config.WORK_DIR / "batch_thumbs"
INPUT_THUMB_DIR = config.WORK_DIR / "input_thumbs"


# ---------------------------------------------------------------------------
# background job
# ---------------------------------------------------------------------------
class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.kind = ""
        self.running = False
        self.lines: list[str] = []
        self.progress = (0, 0)
        self.result: dict = {}
        self.started = 0.0
        self.cancel_event = threading.Event()

    def say(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            for line in str(msg).splitlines() or [""]:
                self.lines.append(f"{stamp} {line}")
            del self.lines[:-2000]

    def start(self, kind: str, fn) -> bool:
        with self.lock:
            if self.running or config.DRAIN_FILE.exists():
                return False
            self.kind, self.running, self.lines, self.progress, self.result = kind, True, [], (0, 0), {}
            # Publish the running flag before the final drain check: a deploy health
            # check can then either see this job or prevent it from starting.
            if config.DRAIN_FILE.exists():
                self.running = False
                return False
            self.started = time.time()
            self.cancel_event = threading.Event()

        def body() -> None:
            generation_token = BRIDGE_GENERATION.set(generation)
            try:
                self.result = fn() or {}
            except mayzing.LoginCancelled as e:
                self.say(str(e))
                self.result = {"ok": False, "cancelled": True}
            except mayzing.VerificationRequired as e:
                self.say(str(e))
                self.result = {"error": str(e), "verification": True}
            except mayzing.SessionExpired as e:
                self.say(str(e))
                self.result = {"error": str(e), "login": True}
            except Exception as e:  # noqa: BLE001
                self.say(f"LỖI: {e.__class__.__name__}: {e}")
                log.error(traceback.format_exc())
                self.result = {"error": str(e)}
            finally:
                BRIDGE_GENERATION.reset(generation_token)
                with self.lock:
                    self.running = False
                self.say(f"--- kết thúc ({round(time.time() - self.started)} giây) ---")
        generation = BRIDGE.generation
        threading.Thread(target=bind_context(body), daemon=True, name=f"job-{kind}").start()
        return True

    def as_json(self, since: int = 0) -> dict:
        with self.lock:
            return {"kind": self.kind, "running": self.running, "total_lines": len(self.lines),
                    "lines": self.lines[since:], "progress": list(self.progress), "result": self.result,
                    "started": self.started}


_default_job = Job()
JOB = LocalProxy(lambda: RUNTIME.get().job if RUNTIME.get() is not None else _default_job)


def busy_response():
    return jsonify({"error": f"Đang chạy việc khác ({JOB.kind}), chờ xong đã."}), 409


# ---------------------------------------------------------------------------
# Mayzing cache (campaign list of the active store)
# ---------------------------------------------------------------------------
_default_campaigns = {}
_default_session = {"checked": 0.0, "ok": None, "error": "", "stores": []}
_default_product_names = {}
_campaigns = LocalProxy(lambda: RUNTIME.get().campaigns if RUNTIME.get() is not None else _default_campaigns)
_session = LocalProxy(lambda: RUNTIME.get().session if RUNTIME.get() is not None else _default_session)
_product_names = LocalProxy(lambda: RUNTIME.get().product_names if RUNTIME.get() is not None else _default_product_names)


class DeviceRuntime:
    def __init__(self, workspace_id):
        self.workspace_id = workspace_id
        self.bridge = BrowserBridge()
        self.job = Job()
        self.review = review.Review()
        self.campaigns = {}
        self.session = {"checked": 0.0, "ok": None, "error": "", "stores": []}
        self.product_names = {}
        self.last_seen = time.monotonic()


DEVICE_RUNTIMES = {}
_runtime_lock = threading.RLock()
_active_mutation_requests = 0
_extension_client_paths = {"/api/bridge/pair", "/api/bridge/poll", "/api/bridge/heartbeat",
                           "/api/bridge/result", "/api/bridge/disconnect"}
_public_paths = {"/api/health", "/api/huongdan", "/api/huongdan/", "/api/huongdan/video.mp4",
                 "/api/huongdan/extension.zip", "/api/bridge/extension.zip"}


def _bind_runtime(runtime):
    g.workspace_token = WORKSPACE_ID.set(runtime.workspace_id)
    g.runtime_token = RUNTIME.set(runtime)
    runtime.last_seen = time.monotonic()


def _device_runtime(workspace_id):
    with _runtime_lock:
        runtime = DEVICE_RUNTIMES.get(workspace_id)
        if runtime is None:
            # Bound memory use; persisted device files survive idle-runtime eviction.
            if len(DEVICE_RUNTIMES) >= 1024:
                idle = [key for key, item in DEVICE_RUNTIMES.items()
                        if not item.job.running and not item.bridge.status()["connected"]
                        and not item.bridge.commands
                        and (not item.bridge.code or time.monotonic() >= item.bridge.code_expires)]
                idle.sort(key=lambda key: DEVICE_RUNTIMES[key].last_seen)
                for key in idle:
                    del DEVICE_RUNTIMES[key]
                    if len(DEVICE_RUNTIMES) < 768:
                        break
                if len(DEVICE_RUNTIMES) >= 1024:
                    abort(503, "Đã đạt giới hạn phiên hoạt động. Thử lại sau.")
            workspace_token = WORKSPACE_ID.set(workspace_id)
            try:
                config.ensure_dirs()
                runtime = DeviceRuntime(workspace_id)
            finally:
                WORKSPACE_ID.reset(workspace_token)
            DEVICE_RUNTIMES[workspace_id] = runtime
        return runtime


def _is_local_request():
    port = str(request.environ.get("SERVER_PORT", config.DASHBOARD_PORT))
    return (request.remote_addr in ("127.0.0.1", "::1") and
            request.host.lower() in (f"127.0.0.1:{port}", f"localhost:{port}", "localhost", "127.0.0.1"))


@app.before_request
def bind_device_request():
    global _active_mutation_requests
    public = request.host.lower() == config.PUBLIC_HOST
    if not public and not _is_local_request():
        abort(403)
    if request.url_rule is None:
        abort(404)
    origin = request.headers.get("Origin", "")
    extension = bool(re.fullmatch(r"chrome-extension://[a-p]{32}", origin))
    extension_path = request.path in _extension_client_paths
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if public and not request.is_json and request.path != "/api/inputs":
            abort(415)
        if request.is_json and not isinstance(request.get_json(silent=True), dict):
            abort(400)
        if public:
            if origin != config.PUBLIC_ORIGIN and not (extension_path and (extension or not origin)):
                abort(403)
        elif origin and not (extension and extension_path):
            try:
                parsed = urlsplit(origin)
                allowed = (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
                           and str(parsed.port or 80) == str(request.environ.get("SERVER_PORT", config.DASHBOARD_PORT)))
            except ValueError:
                allowed = False
            if not allowed:
                abort(403)
        if config.DRAIN_FILE.exists() and request.path not in _extension_client_paths - {"/api/bridge/pair"}:
            return jsonify({"error": "Đang cập nhật ứng dụng. Chờ một chút rồi thử lại."}), 503
        if request.path not in _extension_client_paths - {"/api/bridge/pair"}:
            with _runtime_lock:
                _active_mutation_requests += 1
                g.counted_mutation = True
            if config.DRAIN_FILE.exists():
                return jsonify({"error": "Đang cập nhật ứng dụng. Chờ một chút rồi thử lại."}), 503
    if not public or request.path in _public_paths:
        return
    if extension_path:
        # Extensions intentionally omit dashboard cookies: bind by single-use code or bearer.
        body = request.get_json(silent=True) or {}
        runtime = None
        with _runtime_lock:
            if request.path == "/api/bridge/pair":
                code = str(body.get("code") or "").strip().upper()
                if code.isascii() and len(code) == 12:
                    runtime = next((item for item in DEVICE_RUNTIMES.values() if item.bridge.code
                                    and hmac.compare_digest(code, item.bridge.code)
                                    and time.monotonic() < item.bridge.code_expires), None)
            else:
                try:
                    token = _bridge_token()
                except BridgeError:
                    token = ""
                if token:
                    runtime = next((item for item in DEVICE_RUNTIMES.values() if item.bridge.token
                                    and hmac.compare_digest(token, item.bridge.token)), None)
        if runtime is None:
            return jsonify({"error": "Kết nối không hợp lệ hoặc đã hết hạn. Tạo mã mới rồi ghép lại."}), 401
        _bind_runtime(runtime)
        return
    if extension:
        abort(403)
    workspace_id = session.get("workspace_id", "")
    if not isinstance(workspace_id, str) or not re.fullmatch(r"[a-f0-9]{32}", workspace_id):
        workspace_id = secrets.token_hex(16)
        session.clear()
        session["workspace_id"] = workspace_id
        session.permanent = True
    _bind_runtime(_device_runtime(workspace_id))


@app.after_request
def private_response(response):
    if RUNTIME.get() is not None or request.path.startswith("/api/bridge/") or request.path == "/api/health":
        response.headers["Cache-Control"] = "private, no-store"
        response.vary.add("Cookie")
    return response


@app.teardown_request
def reset_device_request(error=None):
    global _active_mutation_requests
    if getattr(g, "counted_mutation", False):
        with _runtime_lock:
            _active_mutation_requests -= 1
        del g.counted_mutation
    if hasattr(g, "runtime_token"):
        RUNTIME.reset(g.runtime_token)
        WORKSPACE_ID.reset(g.workspace_token)
        del g.runtime_token, g.workspace_token


@app.get("/api/health")
def api_health():
    with _runtime_lock:
        active = sum(int(item.job.running) for item in DEVICE_RUNTIMES.values()) + int(_default_job.running)
    return jsonify({"ok": True, "release": os.environ.get("MAYZING_RELEASE", "development"),
                    "active_jobs": active, "active_requests": _active_mutation_requests})


def campaign_product_options(mz, store: str, group: dict) -> list[dict]:
    cached = _product_names.get(store)
    if not cached or time.time() - cached[0] > config.STORE_CACHE_S:
        try:
            names = mz.base_product_names(store)
        except mayzing.SessionExpired:
            raise
        except mayzing.MayzingError:
            names = {}
        _product_names[store] = (time.time(), names)
    else:
        names = cached[1]
    try:
        observed = json.loads((config.WORK_DIR / 'product-labels.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        observed = {}
    return product_defaults.options(group, names, observed if isinstance(observed, dict) else {})


def campaigns(refresh: bool = False) -> list[dict]:
    store = config.load_settings().get("store_slug") or ""
    if not store:
        return []
    hit = _campaigns.get(store)
    if hit and not refresh and time.time() - hit[0] < config.STORE_CACHE_S:
        return hit[1]
    with mayzing.Mayzing() as mz:
        groups = mz.groups(store)
    groups.sort(key=lambda g: g.get("createdAt") or "", reverse=True)
    _campaigns[store] = (time.time(), groups)
    return groups


def origin_names() -> dict[str, str]:
    store = config.load_settings().get("store_slug") or ""
    hit = _campaigns.get(store)
    if not hit:
        try:
            campaigns()
            hit = _campaigns.get(store)
        except Exception:  # noqa: BLE001
            return {}
    return {g["id"]: f"{g.get('name')} (/{g.get('slug')})" for g in (hit[1] if hit else [])}


# ---------------------------------------------------------------------------
# pages + info
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return send_file(PAGE)


@app.get("/api/huongdan")
@app.get("/api/huongdan/")
def connection_guide():
    return send_file(GUIDE_PAGE)


@app.get("/api/huongdan/video.mp4")
def connection_guide_video():
    # Conditional responses support seeking without downloading the whole video.
    return send_file(GUIDE_VIDEO, mimetype="video/mp4", conditional=True, max_age=86400)


@app.get("/api/huongdan/extension.zip")
def connection_guide_extension():
    # Only distribute the extension's source files, never local connection data.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in ("manifest.json", "background.js", "server.js", "operations.js", "popup.html", "popup.js", "README.md"):
            bundle.write(config.ROOT / "chrome-extension" / name, "chrome-extension/" + name)
    archive.seek(0)
    return send_file(archive, mimetype="application/zip", as_attachment=True,
                     download_name="Mayzing-Session-Bridge.zip")


@app.get("/api/info")
def api_info():
    ok, why = ocr.available()
    settings = config.load_settings()
    rules = [("viết hoa" if (config.TITLE_CASE or "upper").lower() == "upper" else "giữ chữ như in"),
             f"lấy phần đầu quote, tối đa {config.TITLE_MAX_WORDS} từ / {config.TITLE_MAX_LENGTH} ký tự"]
    if config.FORBIDDEN_WORDS:
        rules.append("xoá: " + ", ".join(config.FORBIDDEN_WORDS))
    return jsonify({
        "settings": settings,
        "ocr": {"available": ok, "label": why},
        "session": {"file": mayzing.has_session_file(), **_session},
        "browser": BRIDGE.status(),
        "extension_dir": str(config.ROOT / "chrome-extension"),
        "workspace_id": WORKSPACE_ID.get(),
        "bridge_server": config.PUBLIC_ORIGIN + "/" if RUNTIME.get() is not None else f"http://127.0.0.1:{config.DASHBOARD_PORT}/",
        "canvas": list(config.ARTWORK_CANVAS_SIZE), "artwork_mode": config.ARTWORK_MODE,
        "clone_max": config.CLONE_MAX_PER_TASK,
        "models": [config.CLAUDE_MODEL, config.CLAUDE_HARD_MODEL],
        "title_case": (config.TITLE_CASE or "upper").lower(), "title_rules": ", ".join(rules),
        "title_max_length": config.TITLE_MAX_LENGTH, "title_max_words": config.TITLE_MAX_WORDS,
        "input_dir": str(config.INPUT_DIR), "output_dir": str(config.OUTPUT_DIR),
    })


@app.get("/api/job")
def api_job():
    return jsonify(JOB.as_json(int(request.args.get("since", 0) or 0)))


# ---------------------------------------------------------------------------
# design files
# ---------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    name = Path(name or "").name
    if not name or name in (".", "..") or Path(name).name != name:
        abort(400, "tên file không hợp lệ")
    return name


@app.get("/api/inputs")
def api_inputs():
    known = {i["source"] for i in REVIEW.snapshot()}
    files = []
    for p in sorted(config.INPUT_DIR.iterdir(), key=lambda p: p.name.lower()):
        if p.is_file() and p.suffix.lower() in config.IMAGE_EXTS:
            st = p.stat()
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime, "in_review": p.name in known})
    return jsonify({"files": files})


@app.get("/img/input/<name>")
def img_input(name: str):
    name = _safe_name(name)
    src = config.INPUT_DIR / name
    if not src.exists():
        abort(404)
    thumb = INPUT_THUMB_DIR / (name + ".png")
    if not thumb.exists() or thumb.stat().st_mtime < src.stat().st_mtime:
        from PIL import Image
        thumb.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                im = im.convert("RGBA")       # CMYK JPEG etc.
            im.thumbnail((320, 320))
            im.save(thumb, format="PNG")
    return send_file(thumb, max_age=600)


@app.post("/api/inputs/delete-all")
def api_inputs_delete_all():
    known = {i["source"] for i in REVIEW.snapshot()}
    n = 0
    for p in config.INPUT_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in config.IMAGE_EXTS and p.name not in known:
            p.unlink(missing_ok=True)
            n += 1
    return jsonify({"deleted": n})


@app.post("/api/inputs")
def api_inputs_upload():
    saved = []
    for f in request.files.getlist("files"):
        name = _safe_name(f.filename)
        if Path(name).suffix.lower() not in config.IMAGE_EXTS:
            continue
        dest = config.INPUT_DIR / name
        f.save(dest)
        saved.append(name)
    return jsonify({"saved": saved})


@app.post("/api/inputs/delete")
def api_inputs_delete():
    name = _safe_name((request.get_json(silent=True) or {}).get("name", ""))
    if any(i["source"] == name for i in REVIEW.snapshot()):
        return jsonify({"error": "File đang có thẻ ở tab Duyệt - xoá thẻ trước."}), 400
    (config.INPUT_DIR / name).unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.post("/api/prep/start")
def api_prep_start():
    body = request.get_json(silent=True) or {}
    use_ocr = bool(body.get("ocr", True))
    limit = int(body.get("limit") or 0)
    origin = str(body.get("origin") or "").strip()

    def run():
        def prog(n, total):
            JOB.progress = (n, total)
        n = REVIEW.run_prep(JOB.say, use_ocr=use_ocr, limit=limit, progress=prog, origin=origin)
        return {"new": n}
    if not JOB.start("prep", run):
        return busy_response()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# review cards
# ---------------------------------------------------------------------------
def item_json(i: dict) -> dict:
    out = dict(i)
    out["flag_levels"] = {f: review.flag_level(f) for f in i.get("flags", [])}
    out["auto_origin"] = REVIEW.resolve_origin({**i, "origin": ""}, config.load_settings())
    return out


@app.get("/api/items")
def api_items():
    return jsonify({"items": [item_json(i) for i in REVIEW.snapshot()]})


@app.post("/api/item/<uid>")
def api_item(uid: str):
    changes = request.get_json(silent=True) or {}
    store = config.load_settings().get("store_slug") or ""
    try:
        if "slug" in changes and store and config.SLUG_CHECK_MAYZING and mayzing.has_session_file():
            try:
                with mayzing.Mayzing() as mz:
                    item = REVIEW.update(uid, changes, mz, store)
            except mayzing.MayzingError:
                item = REVIEW.update(uid, changes)
        else:
            item = REVIEW.update(uid, changes)
    except KeyError:
        return jsonify({"error": "không thấy thẻ"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"item": item_json(item)})


@app.post("/api/item/<uid>/remove")
def api_item_remove(uid: str):
    body = request.get_json(silent=True) or {}
    REVIEW.remove(uid, delete_file=bool(body.get("delete_file")))
    return jsonify({"ok": True})


@app.post("/api/items/approve-all")
def api_approve_all():
    return jsonify({"approved": REVIEW.approve_all_ok()})


@app.post("/api/items/discard")
def api_discard():
    if JOB.running and JOB.kind == "prep":
        return busy_response()
    return jsonify({"discarded": REVIEW.discard()})


@app.post("/api/export")
def api_export():
    if JOB.running and JOB.kind == "prep":
        return busy_response()
    try:
        batch, n = REVIEW.export(origin_names(), say=JOB.say)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"batch": batch, "count": n})


@app.get("/img/thumb/<uid>")
def img_thumb(uid: str):
    p = review.THUMB_DIR / f"{_safe_name(uid)}.png"
    if not p.exists():
        abort(404)
    return send_file(p, max_age=3600)


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------
@app.get("/api/batches")
def api_batches():
    out = []
    for b in manifest.batches():
        rows = manifest.read(b)
        counts = {"total": len(rows), "done": sum(manifest.is_done(r) for r in rows),
                  "error": sum(manifest.is_error(r) for r in rows)}
        counts["pending"] = counts["total"] - counts["done"] - counts["error"]
        for r in rows:
            r["has_file"] = (config.OUTPUT_DIR / b / r["filename"]).exists()
        out.append({"batch": b, "rows": rows, "counts": counts,
                    "has_log": (config.LOG_DIR / f"{b}.log").exists()})
    return jsonify({"batches": out})


def _batch_name(b: str) -> str:
    b = _safe_name(b)
    if not manifest.path_for(b).exists():
        abort(404)
    return b


@app.get("/img/batch/<batch>/<fname>")
def img_batch(batch: str, fname: str):
    batch, fname = _batch_name(batch), _safe_name(fname)
    src = config.OUTPUT_DIR / batch / fname
    if not src.exists():
        abort(404)
    thumb = BTHUMB_DIR / batch / fname
    if not thumb.exists() or thumb.stat().st_mtime < src.stat().st_mtime:
        from PIL import Image
        thumb.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.thumbnail((240, 240))
            im.save(thumb, format="PNG")
    return send_file(thumb, max_age=600)


@app.post("/api/batch/<batch>/row")
def api_batch_row(batch: str):
    batch = _batch_name(batch)
    body = request.get_json(silent=True) or {}
    rows = manifest.read(batch)
    row = next((r for r in rows if r["filename"] == body.get("filename")), None)
    if row is None:
        return jsonify({"error": "không thấy dòng"}), 404
    if manifest.is_done(row):
        return jsonify({"error": "Campaign đã tạo xong - sửa trên Mayzing."}), 400
    if "slug" in body:
        new = slugs.clean(str(body["slug"] or ""))
        if not slugs.is_valid(new):
            return jsonify({"error": "URL không hợp lệ (chỉ chữ, số, gạch ngang)."}), 400
        if any(r is not row and r["slug"].lower() == new.lower() for r in rows):
            return jsonify({"error": "URL trùng với dòng khác của batch."}), 400
        row["slug"] = new
        slugs.commit([new])
    if "title" in body:
        from lib import titles
        t, _ = titles.build_title(str(body["title"] or ""))
        if not t:
            return jsonify({"error": "Tiêu đề trống."}), 400
        row["title"] = t
    if body.get("skip"):
        row["status"] = "error: bỏ qua (tay)"
    elif "slug" in body or "title" in body:
        row["status"] = "pending"
    manifest.write(batch, rows)
    return jsonify({"row": row})


@app.post("/api/batch/<batch>/delete")
def api_batch_delete(batch: str):
    batch = _batch_name(batch)
    if JOB.running and JOB.kind == "upload":
        return busy_response()
    shutil.rmtree(config.OUTPUT_DIR / batch, ignore_errors=True)
    shutil.rmtree(BTHUMB_DIR / batch, ignore_errors=True)
    return jsonify({"ok": True})


@app.get("/api/batch/<batch>/log")
def api_batch_log(batch: str):
    batch = _safe_name(batch)
    p = config.LOG_DIR / f"{batch}.log"
    return jsonify({"log": p.read_text(encoding="utf-8", errors="replace")[-60000:] if p.exists() else ""})


@app.post("/api/upload/start")
def api_upload_start():
    _bridge_request(local_only=True)
    if not BRIDGE.status()["automation"]:
        return jsonify({"error": mayzing.AUTOMATION_DISABLED}), 409
    body = request.get_json(silent=True) or {}
    batch = _batch_name(body.get("batch", ""))
    retry = bool(body.get("retry_errors"))
    publish = body.get("publish")
    dry = bool(body.get("dry_run"))

    def run():
        import upload_batch
        res = upload_batch.run(batch, retry_errors=retry, publish=None if publish is None else bool(publish),
                               dry_run=dry, say=JOB.say)
        _campaigns.clear()
        return res
    if not JOB.start("upload", run):
        return busy_response()
    return jsonify({"ok": True})


@app.post("/api/publish")
def api_publish():
    _bridge_request(local_only=True)
    if not BRIDGE.status()["automation"]:
        return jsonify({"error": mayzing.AUTOMATION_DISABLED}), 409
    store = config.load_settings().get("store_slug") or ""
    if not store:
        return jsonify({"error": "Chưa chọn store."}), 400

    def run():
        with mayzing.Mayzing(say=JOB.say, interactive=True) as mz:
            mz.publish(store)
        JOB.say('Đã gửi "Publish changes" - xem Task log Mayzing.')
        return {"ok": True}
    if not JOB.start("publish", run):
        return busy_response()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Mayzing: login, session, store, origins
# ---------------------------------------------------------------------------
def _bridge_request(*, local_only=False):
    """Device-bound production control, or the original loopback-only local flow."""
    if RUNTIME.get() is not None:
        if local_only and request.path in _extension_client_paths:
            abort(403)
        if request.method == "POST" and not request.is_json:
            abort(415)
        return request.get_json(silent=True) or {}
    port = str(request.environ.get("SERVER_PORT", config.DASHBOARD_PORT))
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    if request.host not in (f"127.0.0.1:{port}", f"localhost:{port}", "localhost"):
        abort(403)
    origin = request.headers.get("Origin", "")
    if origin:
        try:
            parsed = urlsplit(origin)
            local = (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
                     and str(parsed.port or 80) == port)
        except ValueError:
            abort(403)
        extension = parsed.scheme == "chrome-extension" and bool(parsed.netloc)
        if not local and (local_only or not extension):
            abort(403)
    if request.method == "POST" and not request.is_json:
        abort(415)
    return request.get_json(silent=True) or {}


def _bridge_token():
    value = request.headers.get("Authorization", "")
    token = value[7:] if value.startswith("Bearer ") else ""
    if not token or not token.isascii() or len(token) > 256:
        raise BridgeError("Thiếu thông tin kết nối Chrome.")
    return token


@app.post("/api/bridge/code")
def api_bridge_code():
    _bridge_request(local_only=True)
    if JOB.running:
        return busy_response()
    response = jsonify(BRIDGE.pairing_code())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/browser/status")
def api_browser_status():
    _bridge_request(local_only=True)
    return jsonify({"browser": BRIDGE.status()})


@app.post("/api/bridge/pair")
def api_bridge_pair():
    body = _bridge_request()
    if JOB.running:
        return busy_response()
    code = str(body.get("code") or "").strip()
    if not code.isascii() or len(code) != 12:
        return jsonify({"error": "Mã kết nối không hợp lệ."}), 400
    try:
        result = BRIDGE.pair(code)
    except BridgeError as exc:
        return jsonify({"error": str(exc)}), 401
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/bridge/<action>")
def api_bridge_client(action):
    body = _bridge_request()
    try:
        token = _bridge_token()
        if action == "poll":
            return jsonify(BRIDGE.poll(token, body.get("page") or {}))
        if action == "heartbeat":
            BRIDGE.heartbeat(token, body.get("page") or {})
        elif action == "result":
            result = body.get("result")
            if not isinstance(result, dict):
                return jsonify({"error": "Kết quả không hợp lệ."}), 400
            BRIDGE.acknowledge(token, str(body.get("id") or ""), result)
        elif action == "disconnect":
            BRIDGE.disconnect(token)
        else:
            abort(404)
    except BridgeError as exc:
        return jsonify({"error": str(exc)}), 401
    return jsonify({"ok": True})


@app.get("/api/bridge/extension.zip")
def api_bridge_extension():
    return connection_guide_extension()


@app.post("/api/automation/enable")
def api_automation_enable():
    _bridge_request(local_only=True)
    if JOB.running:
        return busy_response()
    try:
        result = mayzing.enable_automation()
    except mayzing.MayzingError as exc:
        return jsonify({"error": str(exc), "browser": BRIDGE.status()}), 409
    _accept_session(result)
    return jsonify({**result, "browser": BRIDGE.status(), "settings": config.load_settings()})


@app.post("/api/automation/disable")
def api_automation_disable():
    _bridge_request(local_only=True)
    BRIDGE.disable()
    _session.update(ok=False)
    return jsonify({"ok": True, "browser": BRIDGE.status()})


@app.post("/api/login/start")
def api_login_start():
    if RUNTIME.get() is not None:
        return jsonify({"ok": True, "manual": True, "client": True,
                        "url": config.MAYZING_DASHBOARD + "/dashboard"})
    try:
        mayzing.open_chrome()
    except mayzing.MayzingError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "manual": True})


@app.post("/api/login/cancel")
def api_login_cancel():
    with JOB.lock:
        if not JOB.running or JOB.kind != "login":
            return jsonify({"ok": False, "error": "Không có lượt đăng nhập đang chạy."}), 409
        JOB.cancel_event.set()
    return jsonify({"ok": True})


def _refresh_session() -> dict:
    res = mayzing.check_session()
    _accept_session(res)
    return res


def _accept_session(res: dict) -> None:
    _session.update(checked=time.time(), ok=res["ok"], error=res["error"], stores=res["stores"],
                    verification=res.get("verification", False))
    if res["ok"]:
        settings = config.load_settings()
        stores = res["stores"]
        current = next((s for s in stores if s["slug"] == settings.get("store_slug")), None)
        if current is None and len(stores) == 1:
            current = stores[0]
        updates = {"store_slug": current["slug"] if current else "",
                   "store_name": current.get("name") or "" if current else "",
                   "store_domain": current.get("domainName") or "" if current else ""}
        if settings.get("store_slug") != updates["store_slug"]:
            updates.update(origin_default="", origin_dark="", origin_light="", default_products={})
            _campaigns.clear()
            _product_names.clear()
        config.save_settings(updates)


@app.post("/api/session/check")
def api_session_check():
    res = _refresh_session()
    return jsonify({"ok": res["ok"], "error": res["error"], "stores": res["stores"],
                    "verification": res.get("verification", False),
                    "settings": config.load_settings()})


@app.get("/api/campaigns")
def api_campaigns():
    try:
        groups = campaigns(refresh=request.args.get("refresh") == "1")
    except mayzing.SessionExpired as e:
        return jsonify({"error": str(e), "login": True}), 401
    except mayzing.MayzingError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"campaigns": [{k: g.get(k) for k in ("id", "name", "slug", "createdAt", "clonable",
                                                           "processing", "tags")} | {"products": len(g.get("productIds") or [])}
                                  for g in groups]})


@app.get("/api/campaign/<group_id>/products")
def api_campaign_products(group_id: str):
    _bridge_request(local_only=True)
    store = config.load_settings().get("store_slug") or ""
    try:
        with mayzing.Mayzing() as mz:
            group = mz.group_products(store, group_id)
            choices = campaign_product_options(mz, store, group) if group else []
    except mayzing.MayzingError as exc:
        return jsonify({"error": str(exc)}), 409
    if not group:
        return jsonify({"error": "Không tìm thấy campaign trong store đang chọn."}), 404
    return jsonify({"group": group, "products": choices})


@app.post("/api/campaign/<group_id>/default-product")
def api_default_product(group_id: str):
    _bridge_request(local_only=True)
    if JOB.running:
        return busy_response()
    body = request.get_json(silent=True) or {}
    selected = str(body.get('product_id') or '')
    settings = config.load_settings()
    store = settings.get('store_slug') or ''
    if body.get('for_new'):
        preferences = dict(settings.get('default_products') or {})
        if not selected:
            preferences.pop(group_id, None)
        else:
            try:
                with mayzing.Mayzing() as mz:
                    group = mz.group_products(store, group_id)
                    if not group:
                        raise mayzing.MayzingError('Không tìm thấy campaign gốc trong store.')
                    choice = next((p for p in campaign_product_options(mz, store, group) if p['id'] == selected), None)
                    if not choice or not choice['base_product_id']:
                        raise mayzing.MayzingError('Sản phẩm không thuộc campaign gốc này.')
                    product_defaults.resolve(group, choice['base_product_id'])
            except mayzing.MayzingError as exc:
                return jsonify({'error': str(exc)}), 409
            preferences[group_id] = {'base_product_id': choice['base_product_id'], 'label': choice['label']}
        return jsonify({'ok': True, 'settings': config.save_settings({'default_products': preferences})})
    if not selected:
        return jsonify({'error': 'Chọn sản phẩm hiện đầu tiên.'}), 400

    def run():
        with mayzing.Mayzing() as mz:
            group = mz.group_products(store, group_id)
            if not group:
                raise mayzing.MayzingError('Không tìm thấy campaign trong store.')
            changed = group.get('heroProductId') != selected or (group.get('productIds') or [None])[0] != selected
            result = mz.set_first_product(store, group, selected)
            JOB.say('Đã lưu sản phẩm mặc định của campaign trên Mayzing.' if changed else 'Sản phẩm này đã là mặc định.')
            if changed and body.get('publish'):
                mz.publish(store)
                JOB.say('Đã gửi Publish changes. Kiểm tra link campaign sau khi Mayzing xử lý xong.')
            elif changed:
                JOB.say('Bấm Publish changes để sản phẩm mặc định mới hiển thị với khách.')
        _campaigns.clear()
        return {'ok': True, 'group_id': result['id'], 'product_id': selected}

    if not JOB.start('first_product', run):
        return busy_response()
    return jsonify({'ok': True})


@app.post("/api/settings")
def api_settings():
    if JOB.running:
        return busy_response()
    body = request.get_json(silent=True) or {}
    changes = {}
    for k in ("store_slug", "origin_default", "origin_dark", "origin_light"):
        if k in body:
            changes[k] = str(body[k] or "").strip()
    if "store_slug" in changes:
        st = next((s for s in _session.get("stores") or [] if s["slug"] == changes["store_slug"]), None)
        if RUNTIME.get() is not None and changes["store_slug"] and st is None:
            return jsonify({"error": "Store không thuộc phiên shop đã kết nối trên máy này."}), 403
        if st:
            changes["store_name"] = st.get("name") or ""
            changes["store_domain"] = st.get("domainName") or ""
        if changes["store_slug"] != config.load_settings().get("store_slug"):
            changes.update(origin_default="", origin_dark="", origin_light="", default_products={})
            _campaigns.clear()
            _product_names.clear()
    if "extra_tags" in body:
        tags = body["extra_tags"]
        if isinstance(tags, str):
            tags = tags.split(",")
        changes["extra_tags"] = [t.strip() for t in tags if str(t).strip()]
    for k in ("auto_publish", "ocr"):
        if k in body:
            changes[k] = bool(body[k])
    return jsonify({"settings": config.save_settings(changes)})


# ---------------------------------------------------------------------------
def _acquire_dashboard_lock(port: int):
    """Keep one dashboard per port; duplicate Windows listeners can serve stale jobs."""
    lock_file = config.WORK_DIR / f"dashboard-{port}.lock"
    handle = lock_file.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=config.DASHBOARD_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = f"http://{config.DASHBOARD_HOST}:{args.port}/"
    dashboard_lock = _acquire_dashboard_lock(args.port)
    if dashboard_lock is None:
        print(f"Dashboard dang chay: {url}", flush=True)
        if not args.no_browser:
            mayzing.open_chrome(url)
        return
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(config.LOG_DIR / "dashboard.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"Dashboard: {url}  (giữ cửa sổ này mở; Ctrl+C để tắt)", flush=True)
    if not args.no_browser:
        threading.Timer(1.2, lambda: mayzing.open_chrome(url)).start()
    try:
        app.run(host=config.DASHBOARD_HOST, port=args.port, threaded=True, debug=False, use_reloader=False)
    finally:
        mayzing.shutdown()
        dashboard_lock.close()


if __name__ == "__main__":
    main()
