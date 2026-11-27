"""
Client tối giản cho Telegram Bot API (gọi HTTP trực tiếp bằng requests, không thêm thư viện).
Token bot do admin nhập trên giao diện web (lưu ở CONFIG_FILE); nếu chưa nhập thì dùng
biến môi trường TELEGRAM_BOT_TOKEN.
"""

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from security import redact_secrets

API_BASE = "https://api.telegram.org"
# Giới hạn của Telegram là 4096 ký tự/tin; chừa chỗ cho phần đánh số "(1/3)"
MAX_MESSAGE_CHARS = 3900


CONFIG_FILE = "/opt/vibe-hosting/telegram_config.json"
# Định dạng token BotFather cấp: <id số>:<35 ký tự trở lên>
TOKEN_PATTERN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")

_config_lock = threading.Lock()


def load_config() -> Dict[str, Any]:
    """Cấu hình bot admin lưu từ web: {"token", "username"}. Không có tệp thì trả về {}"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(token: str, username: str) -> None:
    """Ghi tạm rồi đổi tên, quyền 600 vì tệp chứa token bot"""
    with _config_lock:
        os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
        tmp = f"{CONFIG_FILE}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": token, "username": username}, f)
        os.replace(tmp, CONFIG_FILE)


def clear_config() -> bool:
    with _config_lock:
        try:
            os.remove(CONFIG_FILE)
            return True
        except FileNotFoundError:
            return False


def token_source() -> Optional[str]:
    """Token đang dùng lấy từ đâu: "web" (admin nhập), "env" (biến môi trường) hoặc None"""
    if load_config().get("token"):
        return "web"
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        return "env"
    return None


def bot_token() -> str:
    return load_config().get("token") or os.getenv("TELEGRAM_BOT_TOKEN", "")


def mask_token(token: str) -> str:
    """123456789:AAE…wxyz, đủ để nhận ra token nào mà không lộ token"""
    if not token:
        return ""
    bot_id, _, secret = token.partition(":")
    return f"{bot_id}:{secret[:3]}…{secret[-4:]}" if len(secret) > 8 else f"{bot_id}:…"


def is_configured() -> bool:
    return bool(bot_token())


def _call(method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30,
          token: Optional[str] = None) -> Any:
    token = token or bot_token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN chưa được cấu hình")
    resp = requests.post(f"{API_BASE}/bot{token}/{method}", json=payload or {}, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} lỗi: {data.get('description', resp.status_code)}")
    return data.get("result")


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> List[str]:
    """Cắt tin dài thành nhiều phần, ưu tiên cắt ở ranh giới dòng"""
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    total = len(parts)
    return [f"{p}\n({i}/{total})" for i, p in enumerate(parts, 1)]


def inline_keyboard(rows: List[List[Dict[str, str]]]) -> Dict[str, Any]:
    """rows: [[{"text": "...", "callback_data": "..."}]]"""
    return {"inline_keyboard": rows}


def send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    """Gửi tin dạng văn bản thường (không parse Markdown để log/stack trace không làm lỗi định dạng).
    Mọi nội dung đều được che token/mật khẩu trước khi gửi. Bàn phím gắn vào phần cuối cùng."""
    parts = split_message(redact_secrets(text))
    for i, part in enumerate(parts):
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
        if reply_markup and i == len(parts) - 1:
            payload["reply_markup"] = reply_markup
        _call("sendMessage", payload)


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    try:
        _call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)
    except Exception:
        pass


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]}, timeout=10)
    except Exception:
        pass


def clear_keyboard(chat_id: int, message_id: int) -> None:
    """Gỡ nút bấm khỏi tin đã gửi để không bấm duyệt lần hai"""
    try:
        _call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                         "reply_markup": {"inline_keyboard": []}}, timeout=10)
    except Exception:
        pass


def get_updates(offset: Optional[int], poll_timeout: int = 50) -> List[Dict[str, Any]]:
    payload: Dict[str, Any] = {"timeout": poll_timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return _call("getUpdates", payload, timeout=poll_timeout + 10) or []


def get_me(token: Optional[str] = None) -> Dict[str, Any]:
    return _call("getMe", timeout=10, token=token)


# ---- trạng thái chạy của bot ----
# Tiến trình giữ vòng nhận tin ghi nhịp tim vào tệp này; worker nào trả lời API cũng đọc được
STATUS_FILE = "/opt/vibe-hosting/hermes_butler_status.json"
HEARTBEAT_STALE_SECONDS = 120


def write_runtime_status(running: bool, error: str = "") -> None:
    try:
        tmp = f"{STATUS_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"running": running, "heartbeat": time.time(),
                       "error": redact_secrets(error.replace(bot_token() or "\0", "***"))[:300]}, f)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def runtime_status() -> Dict[str, Any]:
    """{"running": bot đang nhận tin, "error": lỗi gần nhất}; nhịp tim quá cũ coi như không chạy"""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"running": False, "error": ""}
    fresh = time.time() - float(data.get("heartbeat", 0)) < HEARTBEAT_STALE_SECONDS
    return {"running": bool(data.get("running")) and fresh, "error": data.get("error", "") if fresh else ""}
