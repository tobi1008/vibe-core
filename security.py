"""
Các hàm bảo mật dùng chung cho Vibe Hosting API:
- Xác thực API Key cho mọi endpoint (trừ GitHub Webhook)
- Xác thực token upload ngắn hạn cho các endpoint nhận file nén (trình duyệt gửi thẳng)
- Kiểm tra tên ứng dụng / URL repository do người dùng gửi lên
- Xác minh chữ ký GitHub Webhook
- Che token/mật khẩu khi ghi log hoặc trả lỗi
"""

import hashlib
import hmac
import os
import re
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()

VIBE_API_KEY = os.getenv("VIBE_API_KEY", "")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# Các endpoint không yêu cầu API Key (được bảo vệ bằng cơ chế riêng)
PUBLIC_PATH_PREFIXES = ("/api/webhooks/",)

# Trình duyệt upload file nén thẳng lên API (không qua Vercel, vốn giới hạn body 4,5 MB),
# nên không có API Key. Dashboard cấp một token ngắn hạn, ký HMAC bằng VIBE_API_KEY và gắn với app_name.
UPLOAD_TOKEN_PATHS = ("/api/deploy-archive", "/api/deploy-archive-with-db")
UPLOAD_TOKEN_HEADER = "X-Upload-Token"
# Token được kiểm tra sau khi FastAPI đọc xong body, nên hạn phải đủ dài cho file lớn trên mạng chậm
UPLOAD_TOKEN_MAX_TTL = 30 * 60
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024

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
        upload_token = request.headers.get(UPLOAD_TOKEN_HEADER)
        if upload_token and request.url.path in UPLOAD_TOKEN_PATHS:
            app_name = verify_upload_token(upload_token)
            if not app_name:
                raise HTTPException(status_code=401, detail="Token upload không hợp lệ hoặc đã hết hạn.")
            request.state.upload_app_name = app_name
        else:
            provided = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(provided.encode(), VIBE_API_KEY.encode()):
                raise HTTPException(status_code=401, detail="API Key không hợp lệ hoặc bị thiếu.")

    for param in ("app_name", "project_name"):
        if param in request.path_params:
            validate_app_name(request.path_params[param])


def _sign_upload(app_name: str, expires_at: int) -> str:
    message = f"archive-upload:{app_name}:{expires_at}".encode()
    return hmac.new(VIBE_API_KEY.encode(), message, hashlib.sha256).hexdigest()


def create_upload_token(app_name: str, ttl: int = 15 * 60) -> str:
    """Định dạng token: <app_name>.<expires_at>.<hmac_hex> (dashboard tạo token cùng định dạng)"""
    expires_at = int(time.time()) + ttl
    return f"{app_name}.{expires_at}.{_sign_upload(app_name, expires_at)}"


def verify_upload_token(token: str) -> Optional[str]:
    """Trả về app_name nếu token đúng chữ ký và còn hạn, ngược lại trả về None"""
    if not VIBE_API_KEY:
        return None
    parts = (token or "").split(".")
    if len(parts) != 3 or not APP_NAME_PATTERN.fullmatch(parts[0]) or not parts[1].isdigit():
        return None
    app_name, expires_at = parts[0], int(parts[1])
    now = int(time.time())
    if expires_at < now or expires_at > now + UPLOAD_TOKEN_MAX_TTL:
        return None
    if not hmac.compare_digest(_sign_upload(app_name, expires_at), parts[2]):
        return None
    return app_name


def require_upload_scope(request: Request, app_name: str) -> None:
    """Request dùng token upload chỉ được deploy đúng app_name đã ghi trong token"""
    scoped = getattr(request.state, "upload_app_name", None)
    if scoped is not None and scoped != app_name:
        raise HTTPException(status_code=403, detail="Token upload không áp dụng cho ứng dụng này.")


def validate_app_name(app_name: Optional[str]) -> str:
    if not app_name or not APP_NAME_PATTERN.fullmatch(app_name):
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
    """So sánh đường dẫn thật (đã resolve symlink) để chặn thoát workspace qua symlink"""
    base = os.path.realpath(base_dir)
    target = os.path.realpath(target_path)
    return target == base or target.startswith(base + os.sep)


def safe_join(base_dir: str, rel_path: str) -> Optional[str]:
    """Ghép đường dẫn do AI/người dùng gửi lên, trả về None nếu nằm ngoài base_dir hoặc chạm vào .git"""
    if not rel_path or touches_git_dir(rel_path):
        return None
    full_path = os.path.join(base_dir, rel_path)
    return full_path if is_inside_dir(base_dir, full_path) and os.path.realpath(full_path) != os.path.realpath(base_dir) else None


def touches_git_dir(rel_path: str) -> bool:
    """Chặn truy cập thư mục .git (chứa remote URL kèm token trong .git/config)"""
    parts = re.split(r"[\\/]+", rel_path or "")
    return ".git" in parts
