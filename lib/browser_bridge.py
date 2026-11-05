"""An in-memory, user-paired relay to one normal Chrome tab. No cookies are exported."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

PAIRING_CODE_TTL_S = 24 * 60 * 60


class BridgeError(RuntimeError):
    pass


@dataclass
class Command:
    operation: str
    payload: dict
    mutating: bool
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    claimed: bool = False
    done: bool = False
    result: dict | None = None


class BrowserBridge:
    def __init__(self):
        self.cv = threading.Condition()
        self.code = ""
        self.code_expires = 0.0
        self.token = ""
        self.generation = 0
        self.last_seen = 0.0
        self.ready = False
        self.enabled = False
        self.extension_version = ''
        self.message = "Chưa kết nối tab Chrome đã đăng nhập."
        self.commands: dict[str, Command] = {}

    def _connected(self):
        return bool(self.token) and time.monotonic() - self.last_seen < 35

    def status(self):
        with self.cv:
            connected = self._connected()
            if not connected:
                self.enabled = False
            return {"mode": "normal", "connected": connected, "ready": connected and self.ready,
                    "automation": connected and self.enabled, "message": self.message,
                    "extension_version": self.extension_version}

    def pairing_code(self):
        with self.cv:
            self.code = secrets.token_hex(6).upper()
            self.code_expires = time.monotonic() + PAIRING_CODE_TTL_S
            return {"code": self.code, "expires_in": PAIRING_CODE_TTL_S}

    def pair(self, code: str):
        with self.cv:
            if not self.code or time.monotonic() >= self.code_expires or not hmac.compare_digest(code.upper(), self.code):
                raise BridgeError("Mã kết nối sai hoặc đã hết hạn. Tạo mã mới trong tool.")
            self.code = ""
            self.token = secrets.token_urlsafe(32)
            self.generation += 1
            self.last_seen = time.monotonic()
            self.ready = self.enabled = False
            self.extension_version = ''
            self.message = "Đã ghép Chrome. Chờ tab Mayzing sẵn sàng."
            self.cv.notify_all()
            return {"token": self.token}

    def _authenticate(self, token: str):
        if not self.token or not hmac.compare_digest(token, self.token):
            raise BridgeError("Kết nối đã hết hiệu lực. Ghép lại Chrome với tool.")

    def heartbeat(self, token: str, page: dict):
        with self.cv:
            self._authenticate(token)
            self.last_seen = time.monotonic()
            self.ready = page.get("ready") is True
            self.extension_version = str(page.get('extensionVersion') or '')[:32]
            self.message = str(page.get("message") or "Đã nối Chrome.")[:300]
            if not self.ready:
                self.enabled = False
            self.cv.notify_all()

    def poll(self, token: str, page: dict, wait_s=10):
        self.heartbeat(token, page)
        with self.cv:
            deadline = time.monotonic() + wait_s
            while True:
                self._authenticate(token)
                for command in self.commands.values():
                    if not command.claimed and not command.done:
                        if command.mutating and not self.enabled:
                            command.done = True
                            command.result = {"kind": "not_ready", "message": "Tự động đã tắt trước khi gửi thao tác."}
                            self.cv.notify_all()
                            continue
                        command.claimed = True
                        return {"command": {"id": command.id, "operation": command.operation,
                                            "payload": command.payload}}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"command": None}
                self.cv.wait(remaining)

    def acknowledge(self, token: str, command_id: str, result: dict):
        with self.cv:
            self._authenticate(token)
            self.last_seen = time.monotonic()
            command = self.commands.get(command_id)
            if command is not None and command.claimed and not command.done:
                command.result, command.done = result, True
                if result.get("kind") in ("verification", "login", "not_ready"):
                    self.enabled = False
                    self.ready = False
                    self.message = str(result.get("message") or "Tab cần bạn đăng nhập / xác minh.")[:300]
                self.cv.notify_all()

    def request(self, operation: str, payload: dict, *, mutating=False, timeout=120, allow_disabled=False):
        with self.cv:
            if not self._connected():
                raise BridgeError("Chưa nối Chrome. Đăng nhập Mayzing rồi kết nối tiện ích với tool.")
            if not self.ready:
                raise BridgeError(self.message or "Hoàn tất đăng nhập / xác minh trong Chrome trước.")
            if not self.enabled and not allow_disabled:
                raise BridgeError("Chưa bật tự động. Bấm Bật tự động upload sau khi kết nối Chrome.")
            generation = self.generation
            command = Command(operation, payload, mutating)
            self.commands[command.id] = command
            self.cv.notify_all()
            deadline = time.monotonic() + timeout
            try:
                while not command.done:
                    if generation != self.generation or not self._connected():
                        self.enabled = False
                        raise BridgeError("Kết nối Chrome bị ngắt. Tool đã dừng; kiểm tra Task log trước khi thử lại.")
                    if not self.ready or (not self.enabled and not allow_disabled):
                        raise BridgeError("Tự động đã dừng. Kiểm tra tab Chrome trước khi tiếp tục.")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.enabled = False
                        if command.claimed and mutating:
                            raise BridgeError("Chưa xác nhận được kết quả thao tác. Tool không gửi lại; kiểm tra Task log Mayzing trước khi tiếp tục.")
                        raise BridgeError("Chrome chưa trả kết quả. Tool đã dừng, hãy kiểm tra tab Mayzing.")
                    self.cv.wait(min(remaining, 1))
                return command.result or {}
            finally:
                self.commands.pop(command.id, None)

    def enable(self):
        with self.cv:
            if not self._connected() or not self.ready:
                raise BridgeError("Kết nối tab Mayzing đã đăng nhập trước khi bật tự động.")
            self.enabled = True
            self.message = "Tự động đã bật cho tab Mayzing đã kết nối."
            self.cv.notify_all()

    def disable(self):
        with self.cv:
            self.enabled = False
            self.message = "Tự động đã tắt. Chrome vẫn mở bình thường."
            self.cv.notify_all()

    def disconnect(self, token: str | None = None):
        with self.cv:
            if token is not None:
                self._authenticate(token)
            self.token = ""
            self.generation += 1
            self.enabled = self.ready = False
            self.message = "Đã ngắt kết nối Chrome."
            self.cv.notify_all()


BRIDGE = BrowserBridge()
