"""
API liên kết Telegram (frontend gọi, có X-API-Key) và khởi động Hermes Butler + Hermes Monitor.
"""

import fcntl
import os
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import builder_core
import telegram_client as tg
import telegram_links

router = APIRouter(prefix="/api/telegram", tags=["Hermes Butler - Telegram"])

LOCK_FILE = "/opt/vibe-hosting/hermes_butler.lock"
_bot_username: Optional[str] = None
_stop = threading.Event()
_lock_handle = None


class LinkCodeRequest(BaseModel):
    user_id: str
    username: Optional[str] = ""


def bot_username() -> Optional[str]:
    global _bot_username
    if _bot_username:
        return _bot_username
    _bot_username = os.getenv("TELEGRAM_BOT_USERNAME") or None
    if not _bot_username and tg.is_configured():
        try:
            _bot_username = tg.get_me().get("username")
        except Exception:
            _bot_username = None
    return _bot_username


@router.post("/link-code")
def create_link_code(req: LinkCodeRequest):
    if not tg.is_configured():
        raise HTTPException(status_code=503, detail="Máy chủ chưa cấu hình bot Telegram.")
    if not req.user_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu user_id")
    result = telegram_links.create_link_code(req.user_id.strip(), (req.username or "").strip())
    username = bot_username()
    result["bot_username"] = username
    result["link"] = f"https://t.me/{username}?start={result['code']}" if username else None
    return {"status": "success", **result}


@router.get("/status")
def link_status(user_id: str = Query(..., min_length=1)):
    return {"status": "success", "configured": tg.is_configured(), "bot_username": bot_username(),
            **telegram_links.link_status(user_id)}


@router.delete("/link")
def unlink(user_id: str = Query(..., min_length=1)):
    removed = telegram_links.unlink_user(user_id)
    return {"status": "success", "removed": removed}


def _acquire_single_instance_lock() -> bool:
    """Chỉ một tiến trình được nhận tin Telegram (tránh nhiều worker cùng getUpdates)"""
    global _lock_handle
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        handle = open(LOCK_FILE, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_handle = handle
    return True


def start_background_services() -> bool:
    """Gọi khi API khởi động. Không có TELEGRAM_BOT_TOKEN hoặc HERMES_BUTLER=0 thì không chạy gì"""
    if not tg.is_configured() or os.getenv("HERMES_BUTLER", "1") == "0":
        return False
    if not _acquire_single_instance_lock():
        builder_core.write_log("hermes-butler", "ℹ️ Tiến trình khác đang chạy Hermes Butler, bỏ qua.")
        return False
    from hermes_butler import butler
    from hermes_monitor import monitor
    threading.Thread(target=butler.poll_forever, args=(_stop,), name="hermes-butler-poll", daemon=True).start()
    threading.Thread(target=monitor.run_forever, args=(_stop,), name="hermes-monitor", daemon=True).start()
    return True


def stop_background_services() -> None:
    _stop.set()
