"""
API key AI riêng của từng user (OpenAI, Anthropic, Google Gemini, API tương thích OpenAI).

- Lưu ở AI_CREDENTIALS_FILE (mode 600), key mã hóa bằng Fernet. Khóa mã hóa lấy từ AI_KEYS_SECRET,
  không có thì dẫn xuất từ VIBE_API_KEY. Đổi khóa thì key cũ không giải mã được nữa (user nhập lại).
- Không bao giờ trả key về: API chỉ trả 4 ký tự cuối.
- Mỗi lần dùng đều kiểm tra credential thuộc đúng chủ app / người yêu cầu (resolve), để user không thể
  dùng key của người khác bằng cách đoán id.
- Chuỗi chọn engine: "byo:<credential_id>" (lưu trong apps_config.json như ai_provider của app).
"""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

AI_CREDENTIALS_FILE = "/opt/vibe-hosting/ai_credentials.json"
PREFIX = "byo:"

PROVIDERS: Dict[str, Dict[str, Any]] = {
    # "example" chỉ để gợi ý trên giao diện; user luôn tự nhập tên model mà key của họ dùng được
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "example": "gpt-4.1-mini",
               "needs_base_url": False},
    "anthropic": {"name": "Anthropic", "base_url": "https://api.anthropic.com", "example": "claude-sonnet-5-5",
                  "needs_base_url": False},
    "gemini": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "example": "gemini-2.5-flash", "needs_base_url": False},
    "openai_compatible": {"name": "OpenAI-compatible", "base_url": "", "example": "deepseek/deepseek-chat",
                          "needs_base_url": True},
}

_MODEL = re.compile(r"^[A-Za-z0-9._:/@-]{1,120}$")
_lock = threading.Lock()


class CredentialError(ValueError):
    pass


def _fernet() -> Fernet:
    secret = os.getenv("AI_KEYS_SECRET") or ("vibe-ai-keys:" + os.getenv("VIBE_API_KEY", ""))
    if secret == "vibe-ai-keys:":
        raise CredentialError("Máy chủ chưa có AI_KEYS_SECRET hoặc VIBE_API_KEY để mã hóa API key.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _load() -> Dict[str, Any]:
    if not os.path.exists(AI_CREDENTIALS_FILE):
        return {}
    with open(AI_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(AI_CREDENTIALS_FILE), exist_ok=True)
    tmp = AI_CREDENTIALS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, AI_CREDENTIALS_FILE)


def public_view(cred: Dict[str, Any]) -> Dict[str, Any]:
    """Thông tin an toàn để trả cho frontend (không có key)"""
    info = PROVIDERS.get(cred["provider"], {})
    return {
        "id": cred["id"],
        "provider": cred["provider"],
        "provider_name": info.get("name", cred["provider"]),
        "label": cred.get("label") or info.get("name", cred["provider"]),
        "model": cred["model"],
        "base_url": cred.get("base_url") or "",
        "key_last4": cred.get("key_last4", ""),
        "is_default": bool(cred.get("is_default")),
        "created_at": cred.get("created_at"),
        "engine": PREFIX + cred["id"],
    }


def display_name(cred: Dict[str, Any]) -> str:
    info = PROVIDERS.get(cred["provider"], {})
    return f"{cred.get('label') or info.get('name', cred['provider'])} ({cred['model']})"


def list_for(user_id: str) -> List[Dict[str, Any]]:
    return [public_view(c) for c in _load().values() if c.get("user_id") == user_id]


def _validate_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", url):
        raise CredentialError("Base URL không hợp lệ (vd https://openrouter.ai/api/v1).")
    return url


def save(user_id: str, provider: str, model: str, api_key: Optional[str] = None,
         base_url: Optional[str] = None, label: Optional[str] = None,
         is_default: bool = False, cred_id: Optional[str] = None) -> Dict[str, Any]:
    """Thêm mới (cred_id None, bắt buộc api_key) hoặc sửa credential của chính user"""
    if not user_id:
        raise CredentialError("Thiếu user_id.")
    if provider not in PROVIDERS:
        raise CredentialError(f"Hãng AI '{provider}' không được hỗ trợ.")
    model = (model or "").strip()
    if not _MODEL.match(model):
        raise CredentialError("Tên model không hợp lệ.")
    if PROVIDERS[provider]["needs_base_url"]:
        base_url = _validate_base_url(base_url or "")
    else:
        base_url = None
    api_key = (api_key or "").strip()
    if api_key and (len(api_key) < 8 or len(api_key) > 400 or any(ch.isspace() for ch in api_key)):
        raise CredentialError("API key không hợp lệ.")

    with _lock:
        data = _load()
        if cred_id:
            cred = data.get(cred_id)
            if not cred or cred.get("user_id") != user_id:
                raise CredentialError("Không tìm thấy key AI này.")
        else:
            if not api_key:
                raise CredentialError("Thiếu API key.")
            cred_id = secrets.token_hex(8)
            cred = {"id": cred_id, "user_id": user_id, "created_at": int(time.time())}
        cred.update({"provider": provider, "model": model, "base_url": base_url,
                     "label": (label or "").strip()[:40] or None})
        if api_key:
            cred["key_enc"] = _fernet().encrypt(api_key.encode()).decode()
            cred["key_last4"] = api_key[-4:]
        mine = [c for c in data.values() if c.get("user_id") == user_id and c["id"] != cred_id]
        if is_default or not any(c.get("is_default") for c in mine):
            for c in mine:
                c["is_default"] = False
            cred["is_default"] = True
        data[cred_id] = cred
        _save(data)
    return public_view(cred)


def delete(user_id: str, cred_id: str) -> bool:
    with _lock:
        data = _load()
        cred = data.get(cred_id)
        if not cred or cred.get("user_id") != user_id:
            return False
        was_default = cred.get("is_default")
        del data[cred_id]
        if was_default:
            rest = [c for c in data.values() if c.get("user_id") == user_id]
            if rest:
                rest[0]["is_default"] = True
        _save(data)
    return True


def set_default(user_id: str, cred_id: str) -> None:
    with _lock:
        data = _load()
        cred = data.get(cred_id)
        if not cred or cred.get("user_id") != user_id:
            raise CredentialError("Không tìm thấy key AI này.")
        for c in data.values():
            if c.get("user_id") == user_id:
                c["is_default"] = c["id"] == cred_id
        _save(data)


def _with_key(cred: Dict[str, Any]) -> Dict[str, Any]:
    try:
        key = _fernet().decrypt(cred["key_enc"].encode()).decode()
    except (InvalidToken, KeyError):
        raise CredentialError("Không giải mã được API key (khóa mã hóa đã đổi?). Hãy nhập lại key trên web.")
    return {**cred, "api_key": key}


def is_byo(engine: Optional[str]) -> bool:
    return bool(engine) and str(engine).startswith(PREFIX)


def resolve(engine: str, owner_user_id: Optional[str]) -> Dict[str, Any]:
    """Credential (kèm key đã giải mã) cho engine "byo:<id>", chỉ khi thuộc đúng owner_user_id"""
    cred_id = str(engine)[len(PREFIX):]
    cred = _load().get(cred_id)
    if not cred or not owner_user_id or cred.get("user_id") != owner_user_id:
        raise CredentialError("Key AI không tồn tại hoặc không thuộc chủ ứng dụng này.")
    return _with_key(cred)


def default_for(user_id: str) -> Optional[Dict[str, Any]]:
    for c in _load().values():
        if c.get("user_id") == user_id and c.get("is_default"):
            return _with_key(c)
    return None


def default_engine(user_id: str) -> Optional[str]:
    """Engine "byo:<id>" của key mặc định của user; None nếu user chưa gắn key (hoặc key hỏng)"""
    for c in _load().values():
        if c.get("user_id") == user_id and c.get("is_default"):
            return PREFIX + c["id"]
    return None


def forget_user(user_id: str) -> int:
    """Xóa mọi key của user (khi user bị xóa)"""
    with _lock:
        data = _load()
        keep = {k: v for k, v in data.items() if v.get("user_id") != user_id}
        removed = len(data) - len(keep)
        if removed:
            _save(keep)
    return removed


# ─────────────────────── Chủ app (để biết key AI của ai được phép dùng cho app) ───────────────────────
# Frontend (đã xác thực, gọi bằng VIBE_API_KEY) đăng ký chủ app trước mỗi lần triển khai. Không nhận
# user_id từ trình duyệt (luồng upload file nén gọi thẳng Vibe Core), nên key "byo:<id>" chỉ dùng được
# khi credential thuộc đúng chủ app đã đăng ký.
APP_OWNERS_FILE = "/opt/vibe-hosting/app_owners.json"


def _base(app_name: str) -> str:
    return re.sub(r"-(be|fe|monorepo)$", "", app_name or "")


def _load_owners() -> Dict[str, str]:
    if not os.path.exists(APP_OWNERS_FILE):
        return {}
    with open(APP_OWNERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def register_app_owner(app_name: str, user_id: str) -> None:
    with _lock:
        data = _load_owners()
        data[_base(app_name)] = user_id
        os.makedirs(os.path.dirname(APP_OWNERS_FILE), exist_ok=True)
        with open(APP_OWNERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def app_owner(app_name: str) -> Optional[str]:
    return _load_owners().get(_base(app_name))


def forget_app(app_name: str) -> None:
    with _lock:
        data = _load_owners()
        if data.pop(_base(app_name), None) is not None:
            with open(APP_OWNERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
