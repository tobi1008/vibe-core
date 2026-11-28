"""
AI Debug ở chế độ Agent: Antigravity / Claude Code được ĐỌC, SỬA file và chạy lệnh để kiểm tra, nhưng chỉ trên
một BẢN SAO mã nguồn của đúng app, bằng user hệ thống không đặc quyền (AI_AGENT_USER, mặc định `vibe-ai`).

Luồng:
  1. start_session: sao chép workspace của app (bỏ .git, node_modules, build...) vào /home/vibe-ai/jobs/<id>/project,
     đổi chủ sở hữu sang vibe-ai, rồi chạy CLI bằng `sudo -n -u vibe-ai env -i ...` với môi trường tối giản (không có
     VIBE_API_KEY, token, kubeconfig). vibe-ai không đọc được /opt/vibe-hosting (chmod 700), /root hay app khác.
  2. Agent sửa file trong bản sao. Xong (hoặc hết thời gian), Vibe Core so bản sao với workspace thật để lấy danh sách
     file thêm/sửa/xóa kèm diff; file manifest K3s (k8s/*.yaml) được kiểm tra trước bằng app_scope.validate_manifest
     (chỉ tài nguyên của app, cấm hostPath/privileged/namespace khác...) và package_limits (không vượt mức đã phân bổ).
  3. apply_session (người dùng bấm "Áp dụng" trên web): ghi file vào workspace thật (edit_file, commit git local),
     apply manifest đã kiểm tra (apply_manifest), rồi build lại nếu có sửa code (rebuild). Dùng lại đúng đường thực thi
     của AI Debug nên mọi kiểm tra phạm vi app và giới hạn gói vẫn áp dụng.
Agent KHÔNG bao giờ chạy kubectl hay đụng tới cụm: chỉ Vibe Core apply, sau khi người dùng duyệt.
"""

import difflib
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import yaml

import app_scope
import package_limits
from builder_core import AGY_MODELS, CLAUDE_MODELS, HYDROGEN_MODELS, K8S_NAMESPACE, write_log

AGENT_USER = os.getenv("AI_AGENT_USER", "vibe-ai")
JOBS_DIR = os.getenv("AI_AGENT_JOBS_DIR", "/home/vibe-ai/jobs")
SESSIONS_DIR = "/opt/vibe-hosting/ai_sessions"
AGENT_TIMEOUT = int(os.getenv("AI_AGENT_TIMEOUT", "600"))
SESSION_TTL = 24 * 3600
MAX_TEXT_BYTES = 512 * 1024
MAX_DIFF_CHARS = 20000
MAX_REPLY_CHARS = 12000
IGNORED_DIRS = {".git", "node_modules", ".next", ".nuxt", "dist", "build", "vendor", "__pycache__", ".venv", "venv",
                ".cache", "target", ".turbo", ".gradle", ".idea", ".vscode", "coverage"}
CLI_BINS = {"agy": "/usr/local/bin/agy", "hydrogen": "/usr/local/bin/agy", "claude": "/usr/local/bin/claude"}

_lock = threading.Lock()
_running: Dict[str, str] = {}  # app -> session id đang chạy


class AgentError(ValueError):
    pass


# ─────────────────────────── Lưu phiên ───────────────────────────

def _session_path(session_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{16}", session_id or ""):
        raise AgentError("Mã phiên không hợp lệ.")
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")


def save_session(session: Dict[str, Any]) -> None:
    os.makedirs(SESSIONS_DIR, mode=0o700, exist_ok=True)
    path = _session_path(session["id"])
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_session(session_id: str) -> Dict[str, Any]:
    path = _session_path(session_id)
    if not os.path.exists(path):
        raise AgentError("Không tìm thấy phiên agent (có thể đã hết hạn).")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def public_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """Dữ liệu trả cho web: bỏ nội dung file đầy đủ, giữ diff"""
    out = {k: v for k, v in session.items() if k != "changes"}
    out["changes"] = [{k: v for k, v in c.items() if k != "new_content"} for c in session.get("changes", [])]
    return out


def prune_sessions(now: Optional[float] = None) -> None:
    now = now or time.time()
    if not os.path.isdir(SESSIONS_DIR):
        return
    for name in os.listdir(SESSIONS_DIR):
        path = os.path.join(SESSIONS_DIR, name)
        try:
            if now - os.path.getmtime(path) > SESSION_TTL:
                os.remove(path)
        except OSError:
            pass


# ─────────────────────────── Hộp cát ───────────────────────────

def available() -> bool:
    try:
        pwd.getpwnam(AGENT_USER)
    except KeyError:
        return False
    return os.path.isdir(JOBS_DIR)


def _ignore(dirpath: str, names: List[str]) -> List[str]:
    return [n for n in names if n in IGNORED_DIRS]


def prepare_copy(workspace: str, job_dir: str) -> str:
    """Bản sao mã nguồn cho agent, thuộc về AGENT_USER"""
    project = os.path.join(job_dir, "project")
    shutil.copytree(workspace, project, ignore=_ignore, symlinks=True)
    subprocess.run(["chown", "-R", f"{AGENT_USER}:{AGENT_USER}", job_dir], check=True, timeout=120)
    os.chmod(job_dir, 0o700)
    return project


def agent_command(engine: str, prompt: str, model: Optional[str]) -> List[str]:
    """Lệnh CLI có công cụ: chạy được vì chỉ thấy bản sao, bằng user không đặc quyền"""
    if engine == "claude":
        return [CLI_BINS["claude"], "-p", prompt, "--model", model or CLAUDE_MODELS[1], "--dangerously-skip-permissions"]
    default = HYDROGEN_MODELS[0] if engine == "hydrogen" else AGY_MODELS[1]
    return [CLI_BINS["agy"], "-p", prompt, "--model", model or default, "--dangerously-skip-permissions"]


def sandboxed(cmd: List[str], cwd: str) -> List[str]:
    home = pwd.getpwnam(AGENT_USER).pw_dir
    env = {
        "HOME": home, "USER": AGENT_USER, "LOGNAME": AGENT_USER, "LANG": "C.UTF-8", "TERM": "dumb",
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin", "KUBECONFIG": "/dev/null", "PWD": cwd,
    }
    return ["sudo", "-n", "-u", AGENT_USER, "--", "env", "-i", *[f"{k}={v}" for k, v in env.items()], *cmd]


# ─────────────────────────── So sánh thay đổi ───────────────────────────

def _walk(root: str) -> Dict[str, str]:
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            files[os.path.relpath(full, root)] = full
    return files


def _read_text(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
        if b"\x00" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_manifest(path: str) -> bool:
    return path.startswith("k8s/") and path.endswith((".yaml", ".yml"))


def check_manifest(app_name: str, content: str, domains: List[str]) -> Optional[str]:
    """Lý do bị chặn của một manifest agent sửa, None nếu hợp lệ"""
    try:
        app_scope.validate_manifest(app_name, content, K8S_NAMESPACE, domains)
        over = package_limits.exceeds_limits(app_name, content)
        return over
    except app_scope.ScopeError as e:
        return str(e)
    except yaml.YAMLError as e:
        return f"YAML không hợp lệ: {str(e)[:150]}"


def collect_changes(app_name: str, original: str, copy: str, domains: List[str]) -> List[Dict[str, Any]]:
    before, after = _walk(original), _walk(copy)
    changes = []
    for rel in sorted(set(before) | set(after)):
        old_path, new_path = before.get(rel), after.get(rel)
        if old_path and new_path:
            old, new = _read_text(old_path), _read_text(new_path)
            if old == new:
                continue
            status = "modified"
        elif new_path:
            old, new, status = "", _read_text(new_path), "added"
        else:
            old, new, status = _read_text(old_path), None, "deleted"
        change: Dict[str, Any] = {"path": rel, "status": status, "kind": "k8s" if is_manifest(rel) else "code"}
        if status != "deleted" and new is None:
            change.update({"blocked": "File nhị phân hoặc quá lớn, không áp dụng qua AI Debug.", "diff": ""})
        else:
            diff = "".join(difflib.unified_diff((old or "").splitlines(True), (new or "").splitlines(True),
                                                fromfile=f"a/{rel}", tofile=f"b/{rel}"))
            change["diff"] = diff[:MAX_DIFF_CHARS] + ("\n… (diff đã rút gọn)" if len(diff) > MAX_DIFF_CHARS else "")
            if status == "deleted":
                change["blocked"] = "Xóa file chưa hỗ trợ áp dụng tự động; hãy xóa thủ công nếu cần."
            else:
                change["new_content"] = new
                if change["kind"] == "k8s":
                    reason = check_manifest(app_name, new, domains)
                    if reason:
                        change["blocked"] = reason
        changes.append(change)
    return changes


# ─────────────────────────── Phiên agent ───────────────────────────

AGENT_PROMPT = """Bạn là AI DevOps Agent của nền tảng Vibe Hosting, đang xử lý ứng dụng "{app}" (thành phần: {component}).

THƯ MỤC LÀM VIỆC hiện tại là BẢN SAO mã nguồn của ứng dụng này (k8s/ chứa manifest K3s: deployment.yaml, service.yaml,
ingress.yaml...; có thể có nixpacks.toml, Dockerfile). Bạn được đọc, sửa, tạo file và chạy lệnh trong thư mục này
(vd cài phụ thuộc, build thử, chạy test) để tìm và sửa lỗi.

GIỚI HẠN:
- Bạn KHÔNG có quyền vào cụm K3s, Docker hay máy chủ: không chạy kubectl/docker/ssh. Mọi thay đổi sẽ được người dùng xem diff
  và bấm duyệt; khi đó Vibe Core mới ghi vào mã thật, apply manifest và build lại.
- Chỉ sửa những gì cần cho yêu cầu. Không đổi tên app, tên Deployment/Service/Ingress, namespace hay tên miền.
  Không thêm hostPath, hostNetwork, privileged, serviceAccount. Phần resources (CPU/RAM) do gói hosting quyết định.
- Không ghi bí mật (mật khẩu, token) vào mã nguồn.

NGỮ CẢNH LÚC CHẠY (do Vibe Core thu thập):
{context}

{history}YÊU CẦU CỦA NGƯỜI DÙNG: {message}

Khi xong, trả lời bằng tiếng Việt, ngắn gọn: đã tìm thấy gì, đã sửa những file nào và vì sao, người dùng cần làm gì tiếp
(vd bấm Áp dụng để Vibe Core build lại). Nếu không cần sửa file, chỉ giải thích."""


def format_context(ctx: Dict[str, Any]) -> str:
    parts = []
    for key, title in (("pod_runtime_logs", "Log container"), ("k8s_events", "Sự kiện K3s"),
                       ("build_logs", "Log build gần nhất"), ("k8s_ingresses", "Ingress"), ("k8s_services", "Service")):
        text = str(ctx.get(key) or "").strip()
        if text:
            parts.append(f"[{title}]\n{text[-6000:]}")
    return "\n\n".join(parts) or "(không thu thập được log)"


def _format_history(history: List[Dict[str, str]]) -> str:
    if not history:
        return ""
    lines = ["HỘI THOẠI TRƯỚC:"]
    for h in history[-8:]:
        who = "Người dùng" if h.get("role") == "user" else "Agent"
        lines.append(f"{who}: {str(h.get('content', ''))[:1500]}")
    return "\n".join(lines) + "\n\n"


def start_session(app_name: str, component: str, message: str, engine: str, user_id: Optional[str],
                  workspace: str, context: Dict[str, Any], domains: List[str],
                  history: Optional[List[Dict[str, str]]] = None, model: Optional[str] = None,
                  run_async: bool = True) -> Dict[str, Any]:
    if engine not in CLI_BINS:
        raise AgentError("Chế độ Agent chỉ hỗ trợ Google Antigravity, Gemini 4 Hydrogen hoặc Claude Code.")
    if not available():
        raise AgentError(f"Máy chủ chưa có user {AGENT_USER} để chạy agent trong hộp cát.")
    if not message.strip():
        raise AgentError("Thiếu nội dung yêu cầu.")
    base = app_scope.base_app_name(app_name)
    with _lock:
        if base in _running:
            raise AgentError("Đang có một phiên agent chạy cho app này, đợi phiên đó xong nhé.")
        session_id = secrets.token_hex(8)
        _running[base] = session_id
    session = {
        "id": session_id, "app": base, "component": component, "user_id": user_id, "engine": engine,
        "message": message[:4000], "status": "running", "started_at": int(time.time()), "finished_at": None,
        "reply": "", "error": None, "changes": [], "applied": False,
    }
    save_session(session)
    prune_sessions()
    prompt = AGENT_PROMPT.format(app=base, component=component, context=format_context(context),
                                 history=_format_history(history or []), message=message.strip()[:4000])
    args = (session, workspace, prompt, domains, model)
    if run_async:
        threading.Thread(target=_run, args=args, daemon=True, name=f"ai-agent-{session_id}").start()
    else:
        _run(*args)
    return session


def _run(session: Dict[str, Any], workspace: str, prompt: str, domains: List[str], model: Optional[str]) -> None:
    job_dir = os.path.join(JOBS_DIR, session["id"])
    app = session["app"]
    try:
        write_log(app, f"🤖 [AI AGENT] Bắt đầu phiên {session['id']} ({session['engine']}) trên bản sao mã nguồn, user {AGENT_USER}.")
        os.makedirs(job_dir, exist_ok=True)
        project = prepare_copy(workspace, job_dir)
        cmd = sandboxed(agent_command(session["engine"], prompt, model), project)
        try:
            res = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=AGENT_TIMEOUT,
                                 stdin=subprocess.DEVNULL)
            output = (res.stdout or "").strip()
            if res.returncode != 0 and not output:
                raise AgentError(ai_sandbox_error(res.stderr))
        except subprocess.TimeoutExpired as e:
            output = (e.stdout or b"").decode("utf-8", "ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")
            output = f"⏱️ Agent chạy quá {AGENT_TIMEOUT // 60} phút nên đã dừng. Những thay đổi đã làm vẫn được liệt kê bên dưới.\n\n{output}"
        session["reply"] = output[-MAX_REPLY_CHARS:]
        session["changes"] = collect_changes(app, workspace, project, domains)
        session["status"] = "done"
        write_log(app, f"🤖 [AI AGENT] Phiên {session['id']} xong: {len(session['changes'])} file thay đổi, chờ người dùng duyệt.")
    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)[:500]
        write_log(app, f"⚠️ [AI AGENT] Phiên {session['id']} lỗi: {str(e)[:300]}")
    finally:
        session["finished_at"] = int(time.time())
        save_session(session)
        shutil.rmtree(job_dir, ignore_errors=True)
        with _lock:
            if _running.get(app) == session["id"]:
                _running.pop(app, None)


def ai_sandbox_error(stderr: str) -> str:
    text = (stderr or "").strip()
    if "a password is required" in text or "sudo:" in text:
        return f"Không chạy được agent bằng user {AGENT_USER} (sudo)."
    if "login" in text.lower() or "auth" in text.lower():
        return f"CLI của user {AGENT_USER} chưa đăng nhập. Admin chạy `sudo -u {AGENT_USER} -i` rồi đăng nhập agy/claude."
    return f"Agent lỗi: {text[-300:] or 'không có thông báo'}"


def apply_session(session_id: str, app_name: str, paths: Optional[List[str]], rebuild: bool,
                  execute: Callable[[str, Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Áp dụng các thay đổi đã chọn. execute(app, action_dict) là execute_remediation_action của AI Debug"""
    session = load_session(session_id)
    if session["app"] != app_scope.base_app_name(app_name):
        raise AgentError("Phiên agent không thuộc ứng dụng này.")
    if session["status"] != "done":
        raise AgentError("Phiên agent chưa xong.")
    if session.get("applied"):
        raise AgentError("Các thay đổi của phiên này đã được áp dụng.")
    selected = [c for c in session["changes"] if (paths is None or c["path"] in paths)]
    results = []
    code_changed = False
    comp = session.get("component") or "be"
    for c in selected:
        if c.get("blocked") or c.get("new_content") is None:
            results.append({"path": c["path"], "status": "skipped", "message": c.get("blocked") or "Không áp dụng được."})
            continue
        res = execute(session["app"], {"action_type": "edit_file", "path": c["path"], "content": c["new_content"],
                                       "target": comp, "description": f"AI Agent sửa {c['path']}"})
        entry = {"path": c["path"], "status": res.get("status"), "message": res.get("message")}
        if res.get("status") == "success":
            if c["kind"] == "k8s":
                applied = execute(session["app"], {"action_type": "apply_manifest", "content": c["new_content"],
                                                   "target": comp, "description": f"Apply {c['path']}"})
                entry["apply"] = {"status": applied.get("status"), "message": applied.get("message")}
            else:
                code_changed = True
        results.append(entry)
    rebuild_result = None
    if rebuild and code_changed:
        rebuild_result = execute(session["app"], {"action_type": "rebuild", "target": comp,
                                                  "description": "Build lại sau khi AI Agent sửa mã"})
    session["applied"] = True
    session["applied_at"] = int(time.time())
    session["apply_results"] = results
    save_session(session)
    write_log(session["app"], f"🤖 [AI AGENT] Đã áp dụng phiên {session_id}: {sum(1 for r in results if r['status'] == 'success')} file.")
    return {"results": results, "rebuild": rebuild_result}
