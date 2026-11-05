"""Normal Chrome login followed by a user-paired extension session.

Requests run inside the selected Mayzing tab. Cookies never leave Chrome.
The tool does not launch or import Playwright.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import config
from lib.browser_bridge import BRIDGE, BridgeError

AUTOMATION_DISABLED = "Kết nối Chrome đã đăng nhập rồi bấm Bật tự động upload."
MANUAL_SESSION_HINT = "Chrome đang dùng chế độ bình thường. Bạn đăng nhập trực tiếp trên Mayzing; tool không kiểm tra phiên đăng nhập."


class MayzingError(RuntimeError):
    pass


class SessionExpired(MayzingError):
    pass


class VerificationRequired(SessionExpired):
    pass


class LoginCancelled(MayzingError):
    pass


def _chrome_executable() -> Path:
    configured = config.BROWSER_EXECUTABLE_PATH
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise MayzingError(f"Không tìm thấy Chrome tại {path}. Kiểm tra BROWSER_EXECUTABLE_PATH trong .env.")
        return path
    candidates = [
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'Google/Chrome/Application/chrome.exe',
    ]
    found = shutil.which('chrome')
    if found:
        candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path
    raise MayzingError("Không tìm thấy Google Chrome. Cài Chrome hoặc đặt BROWSER_EXECUTABLE_PATH trong .env.")


def open_chrome(url: str | None = None) -> None:
    """Use Chrome's normal launch flow and its existing user profile, without automation flags."""
    target = url or config.MAYZING_DASHBOARD + '/dashboard'
    try:
        subprocess.Popen([str(_chrome_executable()), target], close_fds=True)
    except OSError as exc:
        raise MayzingError(f"Không mở được Chrome: {exc}") from exc


def login_interactive(say: Callable[[str], None] = print, timeout_s: int | None = None,
                      cancel_event=None) -> bool:
    """Compatibility for run_login.bat: open normal Chrome and return immediately."""
    if cancel_event is not None and cancel_event.is_set():
        raise LoginCancelled("Đã dừng mở Chrome.")
    open_chrome()
    say("Đã mở Chrome bình thường. Bạn đăng nhập và thao tác trực tiếp trên Mayzing.")
    return True


def shutdown() -> None:
    """Normal Chrome belongs to the user and is left open."""


def has_session_file() -> bool:
    return BRIDGE.status()['automation']


def _decode_reply(reply: dict) -> dict:
    kind = reply.get('kind')
    if kind == 'verification':
        BRIDGE.disable()
        raise VerificationRequired(reply.get('message') or 'Hoàn tất xác minh trong Chrome.')
    if kind in ('not_ready', 'login'):
        BRIDGE.disable()
        raise SessionExpired(reply.get('message') or 'Đăng nhập Mayzing trong Chrome rồi kết nối lại.')
    if kind != 'response':
        BRIDGE.disable()
        raise SessionExpired((reply.get('message') or 'Chưa rõ kết quả thao tác.') + ' Kiểm tra Task log trước khi thử lại.')
    try:
        body = json.loads(reply.get('text') or '')
    except ValueError as exc:
        BRIDGE.disable()
        raise SessionExpired('Mayzing chưa trả dữ liệu. Kiểm tra tab Chrome trước khi tiếp tục.') from exc
    if not isinstance(body, dict):
        raise MayzingError('Dữ liệu Mayzing không hợp lệ.')
    errors = body.get('errors') or []
    if reply.get('status') in (401, 403) or _is_unauthenticated(errors):
        BRIDGE.disable()
        raise SessionExpired('Phiên Mayzing đã hết hạn. Đăng nhập thủ công rồi bật tự động lại.')
    if errors:
        raise MayzingError('; '.join(str(e.get('message') or e) for e in errors))
    return body.get('data') or {}


def _request(operation: str, payload: dict, *, allow_disabled=False):
    if operation not in OPERATIONS:
        raise MayzingError('Lệnh Mayzing không được hỗ trợ.')
    if operation in {'StoreGroupWithProducts', 'StoreBulkPriceEditBaseProductFilters', 'UpdateStoreGroupProductOrder'}:
        version = BRIDGE.status().get('extension_version') or ''
        parts = version.split('.')
        if len(parts) != 3 or not all(p.isdigit() for p in parts) or tuple(map(int, parts)) < (1, 1, 0):
            raise MayzingError('Nạp lại tiện ích Mayzing Session Bridge phiên bản 1.1.0 rồi kết nối lại để chọn sản phẩm mặc định.')
    try:
        reply = BRIDGE.request(operation, payload, mutating=operation in MUTATIONS,
                               timeout=payload.get('timeoutMs', config.GRAPHQL_TIMEOUT_MS) / 1000 + 15,
                               allow_disabled=allow_disabled)
    except BridgeError as exc:
        raise SessionExpired(str(exc)) from exc
    return _decode_reply(reply)


def enable_automation() -> dict:
    data = _request('StoresMinimal', {'variables': {}, 'timeoutMs': 20000}, allow_disabled=True)
    stores = data.get('stores')
    if not isinstance(stores, list):
        raise SessionExpired('Chưa xác nhận được phiên Mayzing. Đăng nhập trong Chrome trước.')
    try:
        BRIDGE.enable()
    except BridgeError as exc:
        raise SessionExpired(str(exc)) from exc
    return {'ok': True, 'stores': stores, 'error': ''}


def check_session() -> dict:
    try:
        with Mayzing() as mz:
            return {'ok': True, 'stores': mz.stores(), 'error': ''}
    except VerificationRequired as exc:
        return {'ok': False, 'stores': [], 'error': str(exc), 'verification': True}
    except MayzingError as exc:
        return {'ok': False, 'stores': [], 'error': str(exc)}


def _is_unauthenticated(errors: list) -> bool:
    for error in errors or []:
        ext = (error or {}).get('extensions') or {}
        if ext.get('code') == 'UNAUTHENTICATED':
            return True
        if (ext.get('originalError') or {}).get('statusCode') in (401, 403):
            return True
        if str((error or {}).get('message', '')).lower() in ('unauthorized', 'forbidden'):
            return True
    return False


def _safe_filename(name: str) -> str:
    bad = '\"\\/\r\n\t'
    return ''.join(' ' if c in bad else c for c in name).strip()[:200] or 'artwork'


def campaign_url(domain: str, slug: str) -> str:
    return f"https://{domain}/{slug}" if domain and slug else ''


OPERATIONS = json.loads((Path(__file__).with_name('mayzing_operations.json')).read_text(encoding='utf-8'))
MUTATIONS = {'ImageUpload', 'CloneGroup', 'UpdateStoreGroup', 'PublishCatalog', 'UpdateStoreGroupProductOrder'}
Q_STORES = OPERATIONS['StoresMinimal']
Q_STORE_GROUPS = OPERATIONS['StoreGroups']
Q_STORE_GROUP = OPERATIONS['StoreGroup']
Q_CHECK_SLUG = OPERATIONS['CheckGroupNameSlugAvailability']
Q_OPERATION_TASK = OPERATIONS['OperationTask']
M_IMAGE_UPLOAD = OPERATIONS['ImageUpload']
M_CLONE_GROUP = OPERATIONS['CloneGroup']
M_UPDATE_GROUP = OPERATIONS['UpdateStoreGroup']
M_PUBLISH = OPERATIONS['PublishCatalog']
RUNNING_STATUSES = {'PENDING', 'NOT_STARTED', 'QUEUED', 'IN_PROGRESS', 'PROCESSING', 'RUNNING', 'SCHEDULED', 'RETRYING'}


class Mayzing:
    """The existing upload interface, using only the manually paired Chrome tab."""
    def __init__(self, say=None, interactive=False):
        self.say = say or (lambda message: None)

    def __enter__(self):
        if not BRIDGE.status()['automation']:
            raise SessionExpired(AUTOMATION_DISABLED)
        return self

    def __exit__(self, *exc):
        pass

    def gql(self, query: str, variables: dict | None = None, op: str | None = None) -> dict:
        if op is None:
            match = re.search(r'(?:query|mutation)\s+(\w+)', query)
            op = match.group(1) if match else ''
        if op not in OPERATIONS or ' '.join(query.split()) != ' '.join(OPERATIONS[op].split()):
            raise MayzingError('Chỉ dùng các thao tác Mayzing đã được cấu hình.')
        return _request(op, {'variables': variables or {}, 'timeoutMs': config.GRAPHQL_TIMEOUT_MS})

    # -- reads and writes ------------------------------------------------------

    def stores(self) -> list[dict]:
        stores = self.gql(Q_STORES, op="StoresMinimal").get("stores") or []
        return stores

    def groups(self, store_slug: str) -> list[dict]:
        data = self.gql(Q_STORE_GROUPS, {"storeSlug": store_slug}, op="StoreGroups")
        return ((data.get("store") or {}).get("storeGroups")) or []

    def group(self, store_slug: str, group_id: str) -> dict | None:
        data = self.gql(Q_STORE_GROUP, {"storeSlug": store_slug, "groupId": group_id}, op="StoreGroup")
        return (data.get("store") or {}).get("storeGroup")

    def group_products(self, store_slug: str, group_id: str) -> dict | None:
        data = self.gql(OPERATIONS['StoreGroupWithProducts'],
                        {'storeSlug': store_slug, 'groupId': group_id}, op='StoreGroupWithProducts')
        return (data.get('store') or {}).get('storeGroup')

    def base_product_names(self, store_slug: str) -> dict[str, str]:
        # Accept either first-page convention used by the catalog filter service.
        for page in (0, 1):
            try:
                data = self.gql(OPERATIONS['StoreBulkPriceEditBaseProductFilters'],
                                {'storeSlug': store_slug, 'page': page, 'pageSize': 100},
                                op='StoreBulkPriceEditBaseProductFilters')
            except SessionExpired:
                raise
            except MayzingError:
                if page == 0:
                    continue
                raise
            values = ((data.get('storeBaseFilters') or {}).get('storeBaseProductsFilters') or {}).get('values') or []
            if values:
                return {str(value['key']): str(value['label']) for value in values if value.get('key') and value.get('label')}
        return {}

    def set_first_product(self, store_slug: str, group: dict, product_id: str) -> dict:
        ids = list(group.get('productIds') or [])
        if product_id not in ids or len(ids) != len(set(ids)):
            raise MayzingError('Sản phẩm không thuộc campaign này hoặc danh sách sản phẩm không hợp lệ.')
        if group.get('processing'):
            raise MayzingError('Campaign còn đang xử lý. Chờ xong rồi chọn sản phẩm mặc định.')
        ordered = [product_id] + [pid for pid in ids if pid != product_id]
        if ordered == ids and group.get('heroProductId') == product_id:
            return group
        data = self.gql(OPERATIONS['UpdateStoreGroupProductOrder'], {'input': {
            'id': group['id'], 'storeSlug': store_slug, 'productIds': ordered,
        }}, op='UpdateStoreGroupProductOrder')
        result = data.get('updateStoreGroupProductOrder') or {}
        if (result.get('id') != group['id'] or result.get('heroProductId') != product_id
                or result.get('productIds') != ordered):
            raise MayzingError('Chưa xác nhận được sản phẩm mặc định. Kiểm tra campaign trên Mayzing trước khi thử lại.')
        return result

    def check_name_slug(self, store_slug: str, slug: str = "", name: str = "") -> tuple[bool, bool]:
        """(name free, slug free) - Mayzing's own availability check (read-only)."""
        data = self.gql(Q_CHECK_SLUG, {"slug": slug, "name": name, "storeSlug": store_slug},
                        op="CheckGroupNameSlugAvailability")
        res = data.get("checkGroupNameSlugAvailability") or {}
        return bool(res.get("name")), bool(res.get("slug"))

    def slug_free(self, store_slug: str, slug: str) -> bool:
        return self.check_name_slug(store_slug, slug=slug)[1]

    def task(self, task_id: str) -> dict:
        return self.gql(Q_OPERATION_TASK, {"id": task_id}, op="OperationTask").get("operationTask") or {}

    # -- writes ---------------------------------------------------------------
    def upload_image(self, path: Path, name: str) -> dict:
        """Upload one artwork to the Artwork library. `name` (without extension) becomes the
        image name - and Bulk clone names each new campaign after its image."""
        path = Path(path)
        ext = path.suffix.lower() or ".png"
        mime = mimetypes.guess_type("x" + ext)[0] or "image/png"
        payload = {'upload': {'b64': base64.b64encode(path.read_bytes()).decode('ascii'),
                              'name': _safe_filename(name) + ext, 'mime': mime},
                   'timeoutMs': config.UPLOAD_TIMEOUT_MS}
        data = _request('ImageUpload', payload)
        img = data.get("imageUpload")
        if not img or not img.get("id"):
            raise MayzingError("upload không trả về id ảnh")
        return img

    def clone_group(self, group_id: str, image_ids: list[str]) -> list[dict]:
        data = self.gql(M_CLONE_GROUP, {"groupId": group_id, "imageIds": image_ids}, op="CloneGroup")
        tasks = data.get("cloneGroup")
        if isinstance(tasks, dict):
            tasks = [tasks]
        if not isinstance(tasks, list) or not tasks or any(
                not isinstance(task, dict) or not task.get("id") for task in tasks):
            raise MayzingError("Bulk clone không trả về danh sách task hợp lệ. Kiểm tra Task log trước khi thử lại.")
        return list({task["id"]: task for task in tasks}.values())

    def wait_task(self, task_id: str, timeout_s: int | None = None,
                  on_progress: Callable[[dict], None] | None = None) -> dict:
        timeout_s = timeout_s or config.CLONE_TASK_TIMEOUT_S
        deadline = time.time() + timeout_s
        last = {}
        while time.time() < deadline:
            last = self.task(task_id)
            if on_progress:
                on_progress(last)
            status = str(last.get("status") or "").upper()
            if last.get("finishedProcessingAt") or (status and status not in RUNNING_STATUSES):
                return last
            time.sleep(config.CLONE_POLL_S)
        raise MayzingError(f"task {task_id} chưa xong sau {timeout_s} giây (trạng thái {last.get('status')})")

    def update_group(self, store_slug: str, group_id: str, name: str, slug: str,
                     tags: list[str]) -> dict:
        data = self.gql(M_UPDATE_GROUP, {"input": {
            "id": group_id, "newName": name, "newSlug": slug, "tags": list(tags), "storeSlug": store_slug,
        }}, op="UpdateStoreGroup")
        return data.get("updateStoreGroup") or {}

    def publish(self, store_slug: str) -> dict:
        return self.gql(M_PUBLISH, {"storeSlug": store_slug}, op="PublishCatalog").get("publishCatalog") or {}
