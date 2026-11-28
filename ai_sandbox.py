"""
Khóa quyền các AI CLI chạy trên máy chủ (Antigravity `agy`, Claude Code `claude`).

Hai CLI này là agent có công cụ (đọc file, chạy lệnh). Máy này giữ kubeconfig quản trị cả cụm, thông tin Docker Hub,
token GitHub, VIBE_API_KEY, mã nguồn của mọi app... nên agent không được chạm tới những thứ đó.

1. cli_env: bỏ mọi biến môi trường bí mật của Vibe Core khỏi môi trường của CLI (giữ lại biến xác thực
   của chính CLI như ANTHROPIC_API_KEY / GEMINI_API_KEY) và trỏ KUBECONFIG về /dev/null.
2. Chạy bằng user không đặc quyền (AI_CLI_USER, mặc định `vibe-ai` nếu máy có) qua `sudo -n -u <user> env -i ...`,
   trong một thư mục trống riêng mỗi lần gọi. User này không đọc được /opt/vibe-hosting (chmod 700) hay /root.
   Chuẩn bị máy chủ: `useradd -m vibe-ai`, `install -d -o vibe-ai -m 700 /home/vibe-ai/jobs`, cài agy/claude vào
   /usr/local/bin, `chmod 700 /opt/vibe-hosting`, đăng nhập CLI bằng `sudo -u vibe-ai -i`.
   Không có user riêng thì CLI chạy bằng root nhưng bị chặn hết công cụ (Claude `--tools ""`, agy `--mode plan`).
AI Debug chế độ Agent (ai_agent_session.py) dùng cùng user nhưng có công cụ, trên bản sao mã nguồn của đúng app.
"""

import os
import pwd
import re
import secrets
import shutil
import subprocess
from typing import Dict, List, Optional

# Hai lớp chặn AI CLI (Antigravity `agy`, Claude Code `claude`) đụng tới máy chủ:
# 1. Chạy bằng user không đặc quyền (AI_CLI_USER, mặc định `vibe-ai` nếu có) trong một THƯ MỤC TRỐNG riêng mỗi lần gọi:
#    user này không đọc được /opt/vibe-hosting (.env, mã nguồn các app), /root (kubeconfig), không có token.
#    Prompt đã chứa sẵn ngữ cảnh nên không cần thư mục mã nguồn.
# 2. Tắt / thu hẹp công cụ: Claude Code `--tools ""`; Antigravity chạy ở chế độ mặc định (hộp cát terminal của agy,
#    không tự duyệt quyền). Chỉ khi không có user riêng mới dùng `--mode plan` (chặn mọi công cụ) — chế độ này
#    khiến model lớn (Gemini 4 Hydrogen) trả về "implementation plan" thay vì câu trả lời nên chỉ là dự phòng.
# Trước đây Antigravity chạy bằng root với --dangerously-skip-permissions: một câu chat ở AI Debug ("kiểm tra dung lượng
# VPS") làm agent chạy lệnh trên máy chủ backend.
CLAUDE_SAFE_FLAGS = ["--tools", ""]
AGY_ROOT_FALLBACK_FLAGS = ["--mode", "plan"]
JOBS_DIR = os.getenv("AI_AGENT_JOBS_DIR", "/home/vibe-ai/jobs")
NO_TOOLS_NOTE = (
    "[Giới hạn] Bạn không được truy cập máy chủ: không chạy lệnh hệ thống, không đọc file ngoài yêu cầu này. "
    "Chỉ trả lời dựa trên dữ liệu có trong yêu cầu. Nếu thiếu dữ liệu, nói rõ cần người dùng cung cấp gì; "
    "không bịa số liệu về máy chủ.\n\n"
)


def agy_flags() -> List[str]:
    """Cờ cho agy: chạy bằng user riêng thì để chế độ mặc định; chạy bằng root thì chặn hết công cụ"""
    return [] if cli_user() else list(AGY_ROOT_FALLBACK_FLAGS)


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
    """User chạy AI CLI: AI_CLI_USER nếu đặt (đặt rỗng hoặc 0 để tắt), mặc định `vibe-ai` nếu máy có user đó"""
    configured = os.getenv("AI_CLI_USER")
    if configured is not None:
        configured = configured.strip()
        return None if configured in ("", "0") else configured
    try:
        pwd.getpwnam("vibe-ai")
        return "vibe-ai"
    except KeyError:
        return None


def wrap(cmd: List[str], env: Dict[str, str], cwd: Optional[str]) -> List[str]:
    """Lệnh thực sự chạy: nguyên lệnh, hoặc qua sudo bằng AI_CLI_USER với môi trường đã lọc"""
    user = cli_user()
    if not user:
        return cmd
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        raise RuntimeError(f"AI_CLI_USER={user} không tồn tại trên máy chủ.")
    run_env = dict(env)
    run_env.update({"HOME": home, "USER": user, "LOGNAME": user,
                    "PATH": ":".join([f"{home}/.local/bin", f"{home}/.npm-global/bin", f"{home}/.gemini/bin",
                                      "/usr/local/bin", "/usr/bin", "/bin"])})
    # Đường dẫn CLI của root (/root/.local/bin/agy) user khác không đọc được: tìm theo PATH của user
    program = os.path.basename(cmd[0])
    return ["sudo", "-n", "-u", user, "--", "env", "-i", *[f"{k}={v}" for k, v in run_env.items()], program, *cmd[1:]]


def run(cmd: List[str], cwd: Optional[str], timeout: int, env: Dict[str, str]) -> subprocess.CompletedProcess:
    safe_env = cli_env(env)
    user = cli_user()
    if not user:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env=safe_env, stdin=subprocess.DEVNULL)
    # Mỗi lần gọi một thư mục trống riêng của user, xóa sau khi xong (không cho CLI thấy mã nguồn hay file máy chủ)
    work = os.path.join(JOBS_DIR, f"q-{secrets.token_hex(6)}")
    os.makedirs(work, exist_ok=True)
    try:
        subprocess.run(["chown", f"{user}:{user}", work], check=False, timeout=30)
        os.chmod(work, 0o700)
        return subprocess.run(wrap(cmd, safe_env, work), cwd=work, capture_output=True, text=True, timeout=timeout,
                              env=safe_env, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(work, ignore_errors=True)
