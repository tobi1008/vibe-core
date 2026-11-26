"""
Client tối giản cho Telegram Bot API (gọi HTTP trực tiếp bằng requests, không thêm thư viện).
Token bot đọc từ biến môi trường TELEGRAM_BOT_TOKEN.
"""

import os
from typing import Any, Dict, List, Optional

import requests

from security import redact_secrets

API_BASE = "https://api.telegram.org"
# Giới hạn của Telegram là 4096 ký tự/tin; chừa chỗ cho phần đánh số "(1/3)"
MAX_MESSAGE_CHARS = 3900


def bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def is_configured() -> bool:
    return bool(bot_token())


def _call(method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
    token = bot_token()
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


def get_me() -> Dict[str, Any]:
    return _call("getMe", timeout=10)
