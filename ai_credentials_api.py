"""
API quản lý key AI riêng của user (prefix /api/ai-credentials). Chỉ frontend gọi (VIBE_API_KEY),
frontend luôn gửi user_id của phiên đăng nhập. Không bao giờ trả API key về, chỉ 4 ký tự cuối.
Key mới hoặc đổi model/base URL đều được gọi thử trước khi lưu.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import ai_credentials
import byo_ai

router = APIRouter(prefix="/api/ai-credentials", tags=["AI Credentials"])


class CredentialInput(BaseModel):
    user_id: str
    id: Optional[str] = None
    provider: str
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    label: Optional[str] = None
    is_default: bool = False


class UserRef(BaseModel):
    user_id: str


def _require_user(user_id: str) -> str:
    if not user_id or not user_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu user_id.")
    return user_id.strip()


@router.get("")
def list_credentials(user_id: str):
    return {
        "status": "success",
        "credentials": ai_credentials.list_for(_require_user(user_id)),
        "providers": [
            {"id": pid, "name": p["name"], "example_model": p["example"], "needs_base_url": p["needs_base_url"]}
            for pid, p in ai_credentials.PROVIDERS.items()
        ],
    }


@router.put("")
def save_credential(req: CredentialInput):
    user_id = _require_user(req.user_id)
    if req.provider not in ai_credentials.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Hãng AI '{req.provider}' không được hỗ trợ.")
    # Gọi thử với cấu hình mới trước khi lưu (dùng key mới, hoặc key đang lưu nếu chỉ đổi model/base URL)
    try:
        if req.api_key:
            probe = {"provider": req.provider, "model": req.model.strip(), "api_key": req.api_key.strip(),
                     "base_url": (req.base_url or "").strip().rstrip("/") or None}
        elif req.id:
            probe = {**ai_credentials.resolve(ai_credentials.PREFIX + req.id, user_id),
                     "provider": req.provider, "model": req.model.strip(),
                     "base_url": (req.base_url or "").strip().rstrip("/") or None}
        else:
            raise HTTPException(status_code=400, detail="Thiếu API key.")
        if ai_credentials.PROVIDERS[req.provider]["needs_base_url"] and not probe["base_url"]:
            raise HTTPException(status_code=400, detail="API tương thích OpenAI cần Base URL.")
        reply = byo_ai.test_connection(probe)
    except ai_credentials.CredentialError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except byo_ai.ByoAIError as e:
        raise HTTPException(status_code=400, detail=f"Gọi thử thất bại, chưa lưu: {e}")
    try:
        saved = ai_credentials.save(user_id, req.provider, req.model, api_key=req.api_key, base_url=req.base_url,
                                    label=req.label, is_default=req.is_default, cred_id=req.id)
    except ai_credentials.CredentialError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "credential": saved, "test_reply": reply}


@router.post("/{cred_id}/test")
def test_credential(cred_id: str, req: UserRef):
    try:
        cred = ai_credentials.resolve(ai_credentials.PREFIX + cred_id, _require_user(req.user_id))
        return {"status": "success", "reply": byo_ai.test_connection(cred)}
    except ai_credentials.CredentialError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except byo_ai.ByoAIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{cred_id}/default")
def make_default(cred_id: str, req: UserRef):
    try:
        ai_credentials.set_default(_require_user(req.user_id), cred_id)
    except ai_credentials.CredentialError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "success"}


@router.delete("/{cred_id}")
def delete_credential(cred_id: str, user_id: str):
    if not ai_credentials.delete(_require_user(user_id), cred_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy key AI này.")
    return {"status": "success"}
