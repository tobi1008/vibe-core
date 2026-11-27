"""
API liên kết Telegram (frontend gọi, có X-API-Key) và khởi động Hermes Butler + Hermes Monitor.

Admin nhập token bot trên trang hồ sơ của frontend (PUT /api/telegram/config); frontend đã kiểm tra
quyền admin trước khi gọi. Bot tự nhận token mới, không cần khởi động lại API.
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
_username_cache: dict = {}
_stop = threading.Event()
_lock_handle = None


class LinkCodeRequest(BaseModel):
    user_id: str
    username: Optional[str] = ""


class BotConfigRequest(BaseModel):
    token: str


def bot_username() -> Optional[str]:
    """Tên bot của token đang dùng: lưu khi admin nhập, hoặc TELEGRAM_BOT_USERNAME, hoặc hỏi getMe"""
    token = tg.bot_token()
    if not token:
        return None
    if tg.token_source() == "web" and tg.load_config().get("username"):
        return tg.load_config()["username"]
    if os.getenv("TELEGRAM_BOT_USERNAME"):
        return os.getenv("TELEGRAM_BOT_USERNAME")
    if token not in _username_cache:
        try:
            _username_cache[token] = tg.get_me().get("username")
        except Exception:
            return None
    return _username_cache[token]


def config_info() -> dict:
    token = tg.bot_token()
    return {
        "configured": bool(token),
        "source": tg.token_source(),
        "bot_username": bot_username(),
        "token_hint": tg.mask_token(token),
        "butler_enabled": os.getenv("HERMES_BUTLER", "1") != "0",
        **tg.runtime_status(),
    }


@router.get("/config")
def get_config():
    return {"status": "success", **config_info()}


@router.put("/config")
def set_config(req: BotConfigRequest):
    token = req.token.strip()
    if not tg.TOKEN_PATTERN.match(token):
        raise HTTPException(status_code=400, detail="Token không đúng định dạng. Token do @BotFather cấp có dạng 123456789:AAH...")
    try:
        me = tg.get_me(token)
    except Exception as e:
        # Lỗi mạng của requests có chứa URL kèm token, không được trả về nguyên văn
        reason = str(e).replace(token, "***")
        if "Unauthorized" in reason:
            reason = "Telegram báo token không hợp lệ hoặc đã bị thu hồi."
        raise HTTPException(status_code=400, detail=f"Không kiểm tra được token: {reason[:200]}")
    if not me.get("is_bot"):
        raise HTTPException(status_code=400, detail="Token này không phải của bot.")
    tg.save_config(token, me.get("username", ""))
    builder_core.write_log("hermes-butler", f"🔑 Admin đã cấu hình bot Telegram @{me.get('username')}.")
    return {"status": "success", **config_info()}


@router.delete("/config")
def delete_config():
    removed = tg.clear_config()
    if removed:
        builder_core.write_log("hermes-butler", "🔑 Admin đã gỡ token bot Telegram nhập trên web.")
    return {"status": "success", "removed": removed, **config_info()}


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
            "running": tg.runtime_status()["running"], **telegram_links.link_status(user_id)}


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
    """Gọi khi API khởi động. HERMES_BUTLER=0 thì không chạy gì. Chưa có token thì hai luồng
    vẫn chạy nhưng chờ, để admin nhập token trên web là bot hoạt động ngay"""
    if os.getenv("HERMES_BUTLER", "1") == "0":
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
