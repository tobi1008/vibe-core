"""
Tra cứu người dùng và quyền sở hữu ứng dụng từ frontend Vibe Hosting.

Vibe Core không lưu tài khoản người dùng; thông tin này nằm trong cơ sở dữ liệu của frontend.
Bot Telegram và bộ giám sát cần biết "ai sở hữu app nào" nên gọi endpoint nội bộ của frontend:
    GET {FRONTEND_URL}/api/internal/telegram/directory   (header X-API-Key = VIBE_API_KEY)
Kết quả được lưu đệm CACHE_SECONDS giây. Khi không gọi được frontend, mọi kiểm tra quyền đều trả về
"không có quyền" (fail closed) thay vì cho phép.
"""

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set

import requests

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://vibe.quyenlt.com").rstrip("/")
DIRECTORY_PATH = "/api/internal/telegram/directory"
CACHE_SECONDS = 60
REQUEST_TIMEOUT = 10

_COMPONENT_SUFFIX = re.compile(r"-(be|fe|db|monorepo)$")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"data": None, "fetched_at": 0.0}


class DirectoryUnavailable(Exception):
    pass


def base_app_name(app_name: str) -> str:
    """Tên gốc của app: bỏ hậu tố component (-be, -fe, -db, -monorepo) do backend tự thêm"""
    return _COMPONENT_SUFFIX.sub("", app_name or "")


def _fetch() -> Dict[str, Any]:
    api_key = os.getenv("VIBE_API_KEY", "")
    if not api_key:
        raise DirectoryUnavailable("VIBE_API_KEY chưa được cấu hình")
    try:
        resp = requests.get(
            f"{FRONTEND_URL}{DIRECTORY_PATH}",
            headers={"X-API-Key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise DirectoryUnavailable(f"Không kết nối được frontend: {e}") from e
    if resp.status_code != 200:
        raise DirectoryUnavailable(f"Frontend trả về HTTP {resp.status_code}")
    payload = resp.json()
    users = {u["id"]: u for u in payload.get("users", []) if u.get("id")}
    projects = [p for p in payload.get("projects", []) if p.get("app_name") and p.get("userId")]
    return {"users": users, "projects": projects}


def get_directory(force: bool = False) -> Dict[str, Any]:
    with _lock:
        fresh = _cache["data"] is not None and time.time() - _cache["fetched_at"] < CACHE_SECONDS
        if fresh and not force:
            return _cache["data"]
    data = _fetch()
    with _lock:
        _cache["data"] = data
        _cache["fetched_at"] = time.time()
    return data


def clear_cache() -> None:
    with _lock:
        _cache["data"] = None
        _cache["fetched_at"] = 0.0


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    return get_directory()["users"].get(user_id)


def is_admin(user_id: str) -> bool:
    user = get_user(user_id)
    return bool(user) and user.get("role") == "admin"


def admin_ids() -> List[str]:
    return [uid for uid, u in get_directory()["users"].items() if u.get("role") == "admin"]


def owned_apps(user_id: str) -> Set[str]:
    """Tên gốc các app user sở hữu (theo bảng Project của frontend)"""
    return {base_app_name(p["app_name"]) for p in get_directory()["projects"] if p["userId"] == user_id}


def owners_of(app_name: str) -> List[str]:
    base = base_app_name(app_name)
    return sorted({p["userId"] for p in get_directory()["projects"] if base_app_name(p["app_name"]) == base})


def can_access(user_id: str, app_name: str) -> bool:
    """User có quyền thao tác app này không (admin: mọi app). Lỗi tra cứu ⇒ không có quyền"""
    try:
        if not get_user(user_id):
            return False
        if is_admin(user_id):
            return True
        return base_app_name(app_name) in owned_apps(user_id)
    except DirectoryUnavailable:
        return False
