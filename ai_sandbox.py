"""
Khóa quyền các AI CLI chạy trên máy chủ (Antigravity `agy`, Claude Code `claude`).

Hai CLI này là agent có công cụ (đọc file, chạy lệnh), lại chạy trong thư mục mã nguồn của user: nội dung repo
(README, comment...) có thể dụ agent chạy lệnh. Máy này giữ kubeconfig quản trị cả cụm, thông tin Docker Hub,
token GitHub, VIBE_API_KEY... nên agent không được chạm tới những thứ đó.

1. cli_env: bỏ mọi biến môi trường bí mật của Vibe Core khỏi môi trường của CLI (giữ lại biến xác thực
   của chính CLI như ANTHROPIC_API_KEY / GEMINI_API_KEY) và trỏ KUBECONFIG về /dev/null.
2. AI_CLI_USER (khuyến nghị): chạy CLI bằng một user hệ thống không đặc quyền qua `sudo -n -u <user>`.
   User này không đọc được /root (kubeconfig, ~/.docker), /opt/vibe-hosting/.env hay workspace của app khác;
   chỉ được cấp quyền ĐỌC thư mục mã nguồn của đúng app đang xử lý (setfacl). Cần chuẩn bị trên máy chủ:
       useradd -m -s /bin/bash vibe-ai
       (đăng nhập agy / claude bằng user vibe-ai)
       echo 'root ALL=(vibe-ai) NOPASSWD: ALL' > /etc/sudoers.d/vibe-ai
   Để trống AI_CLI_USER thì CLI chạy như trước (chỉ áp dụng bước 1).
"""

import os
import pwd
import re
import shutil
import subprocess
from typing import Dict, List, Optional

# Chạy AI CLI KHÔNG CÓ CÔNG CỤ: prompt đã chứa sẵn ngữ cảnh (cây thư mục, file cấu hình, log, sự kiện K3s),
# model chỉ cần đọc và trả lời. Trước đây Antigravity chạy với --dangerously-skip-permissions nên một câu chat ở
# AI Debug ("kiểm tra dung lượng VPS") làm agent chạy lệnh bằng root trên máy chủ.
#   - Antigravity: --mode plan (mọi yêu cầu chạy lệnh / đọc file bị từ chối tự động khi chạy headless)
#   - Claude Code: --tools "" (không có công cụ nào)
AGY_SAFE_FLAGS = ["--mode", "plan"]
CLAUDE_SAFE_FLAGS = ["--tools", ""]
NO_TOOLS_NOTE = (
    "[Giới hạn] Bạn KHÔNG có công cụ: không chạy lệnh, không đọc/ghi file, không truy cập máy chủ. "
    "Chỉ trả lời dựa trên dữ liệu có trong yêu cầu này. Nếu thiếu dữ liệu, nói rõ cần người dùng cung cấp gì; "
    "không bịa số liệu về máy chủ.\n\n"
)


def safe_prompt(prompt: str) -> str:
    return NO_TOOLS_NOTE + prompt


_SENSITIVE = re.compile(r"SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|PRIVATE|_KEY$|^KEY$|DATABASE_URL|KUBECONFIG|DSN",
                        re.IGNORECASE)
# Biến xác thực của chính các CLI: giữ lại để CLI còn đăng nhập được
_CLI_AUTH = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY",
             "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"}


def cli_env(env: Dict[str, str]) -> Dict[str, str]:
    clean = {k: v for k, v in env.items() if k in _CLI_AUTH or not _SENSITIVE.search(k)}
    clean["KUBECONFIG"] = "/dev/null"
    return clean


def cli_user() -> Optional[str]:
    user = os.getenv("AI_CLI_USER", "").strip()
    return user or None


def _grant_read(path: Optional[str], user: str) -> None:
    """Cho user đọc đúng thư mục mã nguồn đang xử lý (và đi qua các thư mục cha)"""
    if not path or not os.path.isdir(path) or not shutil.which("setfacl"):
        return
    subprocess.run(["setfacl", "-R", "-m", f"u:{user}:rX", path], capture_output=True, timeout=60)
    parent = os.path.dirname(os.path.abspath(path))
    while parent and parent != "/":
        subprocess.run(["setfacl", "-m", f"u:{user}:x", parent], capture_output=True, timeout=10)
        parent = os.path.dirname(parent)


def wrap(cmd: List[str], env: Dict[str, str], cwd: Optional[str]) -> List[str]:
    """Lệnh thực sự chạy: nguyên lệnh, hoặc qua sudo bằng AI_CLI_USER với môi trường đã lọc"""
    user = cli_user()
    if not user:
        return cmd
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        raise RuntimeError(f"AI_CLI_USER={user} không tồn tại trên máy chủ.")
    _grant_read(cwd, user)
    run_env = dict(env)
    run_env.update({"HOME": home, "USER": user, "LOGNAME": user,
                    "PATH": ":".join([f"{home}/.local/bin", f"{home}/.npm-global/bin", f"{home}/.gemini/bin",
                                      "/usr/local/bin", "/usr/bin", "/bin"])})
    # Đường dẫn CLI của root (/root/.local/bin/agy) user khác không đọc được: tìm theo PATH của user
    program = os.path.basename(cmd[0])
    return ["sudo", "-n", "-u", user, "--", "env", "-i", *[f"{k}={v}" for k, v in run_env.items()], program, *cmd[1:]]


def run(cmd: List[str], cwd: Optional[str], timeout: int, env: Dict[str, str]) -> subprocess.CompletedProcess:
    safe_env = cli_env(env)
    return subprocess.run(wrap(cmd, safe_env, cwd), cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env=safe_env, stdin=subprocess.DEVNULL)
