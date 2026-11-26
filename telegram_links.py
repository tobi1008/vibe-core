"""
Liên kết tài khoản Vibe Hosting với Telegram.

Luồng liên kết:
1. Người dùng bấm "Liên kết Telegram" trên trang hồ sơ, frontend gọi POST /api/telegram/link-code
   kèm user_id của tài khoản đang đăng nhập.
2. Vibe Core sinh mã ngẫu nhiên dùng một lần, hết hạn sau LINK_CODE_TTL giây.
3. Người dùng mở bot bằng đường dẫn t.me/<bot>?start=<mã>; bot đổi mã lấy liên kết chat_id ↔ user_id.

Dữ liệu được lưu trong tệp JSON trên máy chủ, ghi theo kiểu ghi tạm rồi đổi tên để không hỏng tệp.
"""

import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

LINKS_FILE = "/opt/vibe-hosting/telegram_links.json"
LINK_CODE_TTL = 10 * 60
# Bỏ các ký tự dễ nhầm (0/O, 1/l/I) để người dùng gõ tay được nếu cần
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_CODE_LENGTH = 10

_lock = threading.Lock()


def _empty() -> Dict[str, Any]:
    return {"chats": {}, "codes": {}}


def _load() -> Dict[str, Any]:
    if not os.path.exists(LINKS_FILE):
        return _empty()
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("chats", {})
        data.setdefault("codes", {})
        return data
    except Exception:
        return _empty()


def _save(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(LINKS_FILE) or ".", exist_ok=True)
    tmp = f"{LINKS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LINKS_FILE)


def _drop_expired_codes(data: Dict[str, Any], now: float) -> None:
    data["codes"] = {c: v for c, v in data["codes"].items() if v.get("expires_at", 0) > now}


def create_link_code(user_id: str, username: str = "") -> Dict[str, Any]:
    """Sinh mã liên kết mới cho user; mã cũ chưa dùng của user này bị hủy"""
    if not user_id:
        raise ValueError("Thiếu user_id")
    now = time.time()
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    with _lock:
        data = _load()
        _drop_expired_codes(data, now)
        data["codes"] = {c: v for c, v in data["codes"].items() if v.get("user_id") != user_id}
        data["codes"][code] = {"user_id": user_id, "username": username, "expires_at": now + LINK_CODE_TTL}
        _save(data)
    return {"code": code, "expires_in": LINK_CODE_TTL}


def consume_link_code(code: str, chat_id: int, telegram_name: str = "") -> Optional[Dict[str, Any]]:
    """Đổi mã lấy liên kết. Trả về thông tin liên kết, hoặc None nếu mã sai/hết hạn/đã dùng"""
    code = (code or "").strip().lower()
    now = time.time()
    with _lock:
        data = _load()
        _drop_expired_codes(data, now)
        entry = data["codes"].pop(code, None)
        if not entry:
            _save(data)
            return None
        link = {
            "user_id": entry["user_id"],
            "username": entry.get("username", ""),
            "telegram_name": telegram_name,
            "linked_at": int(now),
        }
        data["chats"][str(chat_id)] = link
        _save(data)
    return link


def get_link(chat_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        return _load()["chats"].get(str(chat_id))


def chats_for_user(user_id: str) -> List[int]:
    with _lock:
        chats = _load()["chats"]
    return [int(cid) for cid, link in chats.items() if link.get("user_id") == user_id]


def all_links() -> Dict[int, Dict[str, Any]]:
    with _lock:
        return {int(cid): link for cid, link in _load()["chats"].items()}


def link_status(user_id: str) -> Dict[str, Any]:
    with _lock:
        chats = _load()["chats"]
    linked = [
        {"telegram_name": link.get("telegram_name", ""), "linked_at": link.get("linked_at")}
        for link in chats.values() if link.get("user_id") == user_id
    ]
    return {"linked": bool(linked), "chats": linked}


def unlink_chat(chat_id: int) -> bool:
    with _lock:
        data = _load()
        removed = data["chats"].pop(str(chat_id), None) is not None
        if removed:
            _save(data)
    return removed


def unlink_user(user_id: str) -> int:
    with _lock:
        data = _load()
        before = len(data["chats"])
        data["chats"] = {cid: link for cid, link in data["chats"].items() if link.get("user_id") != user_id}
        removed = before - len(data["chats"])
        if removed:
            _save(data)
    return removed
