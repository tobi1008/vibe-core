"""
Các hàm bảo mật dùng chung cho Vibe Hosting API:
- Xác thực API Key cho mọi endpoint (trừ GitHub Webhook)
- Kiểm tra tên ứng dụng / URL repository do người dùng gửi lên
- Xác minh chữ ký GitHub Webhook
- Che token/mật khẩu khi ghi log hoặc trả lỗi
"""

import hashlib
import hmac
import os
import re
from typing import Optional

from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()

VIBE_API_KEY = os.getenv("VIBE_API_KEY", "")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# Các endpoint không yêu cầu API Key (được bảo vệ bằng cơ chế riêng)
PUBLIC_PATH_PREFIXES = ("/api/webhooks/",)

# Tên app dùng làm tên tài nguyên K8s, tên thư mục và ghép vào lệnh shell,
# nên chỉ cho phép chữ thường, số và dấu gạch ngang (chuẩn DNS label của K8s)
APP_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")

# https://<token>@github.com/... hoặc https://user:pass@host/...
_URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)[^/\s@]+@")
_GITHUB_TOKEN_PATTERN = re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")


def require_api_key(request: Request):
    """
    Dependency toàn cục: yêu cầu header X-API-Key khớp với VIBE_API_KEY.
    Đồng thời kiểm tra các path param app_name / project_name.
    """
    if not request.url.path.startswith(PUBLIC_PATH_PREFIXES):
        if not VIBE_API_KEY:
            raise HTTPException(status_code=503, detail="Máy chủ chưa cấu hình VIBE_API_KEY.")
        provided = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(provided.encode(), VIBE_API_KEY.encode()):
            raise HTTPException(status_code=401, detail="API Key không hợp lệ hoặc bị thiếu.")

    for param in ("app_name", "project_name"):
        if param in request.path_params:
            validate_app_name(request.path_params[param])


def validate_app_name(app_name: Optional[str]) -> str:
    if not app_name or not APP_NAME_PATTERN.match(app_name):
        raise HTTPException(
            status_code=400,
            detail="Tên ứng dụng không hợp lệ: chỉ gồm chữ thường, số, dấu '-' (tối đa 40 ký tự, không bắt đầu/kết thúc bằng '-')."
        )
    return app_name


def validate_repo_url(repo_url: Optional[str]) -> str:
    """Chỉ chấp nhận URL https:// để chặn clone file cục bộ hoặc chèn tham số vào lệnh git"""
    url = (repo_url or "").strip()
    if not url.startswith("https://") or any(c.isspace() for c in url):
        raise HTTPException(status_code=400, detail="URL repository phải bắt đầu bằng https://")
    return url


def verify_github_signature(body: bytes, signature_header: Optional[str]) -> bool:
    """
    Xác minh header X-Hub-Signature-256 của GitHub.
    Nếu chưa cấu hình GITHUB_WEBHOOK_SECRET thì bỏ qua (tương thích các webhook cũ).
    """
    if not GITHUB_WEBHOOK_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def redact_secrets(text: str) -> str:
    """Che thông tin đăng nhập trong URL và GitHub token trước khi ghi log / trả về client"""
    if not text:
        return text
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1***@", text)
    return _GITHUB_TOKEN_PATTERN.sub("***", text)


def safe_upload_filename(filename: Optional[str]) -> str:
    """Giữ lại chữ, số, '.', '_', '-' trong tên file upload (tên file được ghép vào lệnh shell khi nạp backup)"""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or ""))
    return name.lstrip(".") or "upload"


def is_inside_dir(base_dir: str, target_path: str) -> bool:
    base = os.path.abspath(base_dir)
    target = os.path.abspath(target_path)
    return target == base or target.startswith(base + os.sep)


def touches_git_dir(rel_path: str) -> bool:
    """Chặn truy cập thư mục .git (chứa remote URL kèm token trong .git/config)"""
    parts = re.split(r"[\\/]+", rel_path or "")
    return ".git" in parts
