"""
Model AI mà Hermes dùng cho từng người dùng trên Telegram (chọn bằng lệnh /ai).

- hermes   : model miễn phí qua Hermes gateway (nhanh, mặc định khi user chưa gắn key riêng)
- agy      : Google Antigravity – Gemini 3.8 Flash
- hydrogen : Gemini 4 Hydrogen (qua Antigravity)
- claude   : Claude Code
- key      : API key riêng mặc định của user (OpenAI / Anthropic / Gemini / OpenAI-compatible)
Áp dụng cho phần trò chuyện (điều phối công cụ) và chẩn đoán /debug. AI Debug chế độ Agent (/aidebug) chọn model riêng.
Antigravity / Claude Code chạy bằng user vibe-ai trong thư mục trống, không có quyền vào máy chủ (ai_sandbox).
"""

import json
import os
import threading
from typing import Dict, Optional

import ai_credentials

PREFS_FILE = "/opt/vibe-hosting/hermes_prefs.json"

ENGINES: Dict[str, str] = {
    "hermes": "Hermes (model miễn phí, nhanh)",
    "agy": "Gemini 3.8 Flash (Antigravity)",
    "hydrogen": "Gemini 4 Hydrogen (Antigravity)",
    "claude": "Claude Code",
    "key": "Key AI riêng của bạn",
}

_lock = threading.Lock()


def _load() -> Dict[str, Dict[str, str]]:
    if not os.path.exists(PREFS_FILE):
        return {}
    with open(PREFS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_engine(user_id: str) -> str:
    """Model đang chọn; chưa chọn thì dùng key riêng nếu có, không thì Hermes"""
    chosen = (_load().get(user_id) or {}).get("engine")
    if chosen == "key" and not ai_credentials.default_engine(user_id):
        chosen = None  # key đã bị xóa
    if chosen in ENGINES:
        return chosen
    return "key" if ai_credentials.default_engine(user_id) else "hermes"


def set_engine(user_id: str, engine: str) -> str:
    if engine not in ENGINES:
        raise ValueError(f"Model '{engine}' không hợp lệ.")
    if engine == "key" and not ai_credentials.default_engine(user_id):
        raise ValueError("Bạn chưa gắn key AI riêng. Gắn ở trang Integrations trên web trước.")
    with _lock:
        data = _load()
        data.setdefault(user_id, {})["engine"] = engine
        os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    return engine


def provider_for(user_id: str) -> Optional[str]:
    """Chuỗi ai_provider cho builder_core.call_ai_*: None = Hermes gateway"""
    engine = get_engine(user_id)
    if engine == "hermes":
        return None
    if engine == "key":
        return ai_credentials.default_engine(user_id)
    return engine


def label(user_id: str) -> str:
    engine = get_engine(user_id)
    if engine == "key":
        engine_id = ai_credentials.default_engine(user_id)
        cred = next((c for c in ai_credentials.list_for(user_id) if c["engine"] == engine_id), None)
        if cred:
            return f"Key AI riêng của bạn – {cred['provider_name']} {cred['model']}"
    return ENGINES[engine]
