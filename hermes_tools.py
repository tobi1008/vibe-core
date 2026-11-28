"""
Bộ công cụ của Hermes Butler (quản gia hệ thống qua Telegram).

Mỗi công cụ là một hàm Python do Vibe Core thực thi; Hermes LLM chỉ được chọn tên công cụ và tham số.
- Công cụ đọc (mutating=False): chạy ngay, trả kết quả dạng văn bản cho Hermes đọc tiếp.
- Công cụ thay đổi hệ thống (mutating=True): KHÔNG chạy ngay; bot gửi nút "Duyệt / Bỏ qua" và chỉ
  thực thi khi chính người dùng đã liên kết bấm Duyệt (xem hermes_butler.py).

Quyền: trước khi chạy bất kỳ công cụ nào có tham số app, hàm check_access kiểm tra user sở hữu app
(admin được mọi app). Quyết định quyền nằm ở mã nguồn, không phụ thuộc vào việc Hermes chọn gì.
"""

import json
import re
import os
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import requests

import ai_credentials
import app_scope
import builder_core
import cicd_core
import hermes_verifier
import user_directory
from security import redact_secrets, validate_app_name

TOOL_OUTPUT_LIMIT = 3500


class ToolError(Exception):
    """Lỗi hiển thị được cho người dùng / Hermes (tham số sai, không có quyền...)"""


# ─────────────────────────── Tiện ích chung ───────────────────────────

def _clip(text: str, limit: int = TOOL_OUTPUT_LIMIT) -> str:
    text = redact_secrets(text or "")
    return text if len(text) <= limit else "…" + text[-limit:]


def load_all_configs() -> Dict[str, Dict[str, Any]]:
    path = cicd_core.CONFIG_DB_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def all_app_names() -> List[str]:
    """Tên gốc của mọi app đang có cấu hình trên Vibe Core"""
    return sorted({user_directory.base_app_name(name) for name in load_all_configs()})


def ingress_hosts(app_name: str) -> List[str]:
    """Tên miền trong Ingress của app trên K3s (cho app triển khai trước khi cấu hình lưu tên miền)"""
    try:
        res = subprocess.run(["kubectl", "get", "ingress", "-n", builder_core.K8S_NAMESPACE, "-o", "json"],
                             capture_output=True, text=True, timeout=20)
        items = json.loads(res.stdout or "{}").get("items", []) if res.returncode == 0 else []
    except Exception:
        return []
    hosts: List[str] = []
    for item in items:
        if not app_scope.owns_resource(app_name, item.get("metadata", {}).get("name", "")):
            continue
        for rule in (item.get("spec") or {}).get("rules") or []:
            h = (rule.get("host") or "").strip().lower()
            if h and h not in hosts:
                hosts.append(h)
    return hosts


def app_domains(app_name: str) -> List[str]:
    configs = load_all_configs()
    domains: List[str] = []
    for name, cfg in configs.items():
        if user_directory.base_app_name(name) != app_name:
            continue
        for key in ("app_domain", "fe_domain", "be_domain"):
            d = (cfg.get(key) or "").strip()
            if d and d not in domains:
                domains.append(d)
    return domains or ingress_hosts(app_name)


def visible_apps(user_id: str) -> List[str]:
    if user_directory.is_admin(user_id):
        return sorted(set(all_app_names()) | user_directory.owned_apps(user_id))
    return sorted(user_directory.owned_apps(user_id))


def check_access(user_id: str, app_name: Any) -> str:
    if not isinstance(app_name, str) or not app_name.strip():
        raise ToolError("Thiếu tên ứng dụng (app).")
    app_name = app_name.strip().lower()
    try:
        validate_app_name(app_name)
    except Exception:
        raise ToolError(f"Tên ứng dụng '{app_name}' không hợp lệ.")
    app_name = user_directory.base_app_name(app_name)
    if not user_directory.can_access(user_id, app_name):
        raise ToolError(f"Bạn không có quyền với ứng dụng '{app_name}' hoặc ứng dụng không tồn tại.")
    return app_name


def _component(args: Dict[str, Any]) -> Optional[str]:
    comp = (args.get("component") or "").strip().lower()
    if comp in ("", "none", "null"):
        return None
    if comp not in ("be", "fe"):
        raise ToolError("component chỉ nhận 'be' hoặc 'fe' (bỏ trống với app một thành phần).")
    return comp


def _deployments(app_name: str) -> List[str]:
    return [w.split("/", 1)[1] for w in hermes_verifier.discover_workloads(app_name) if w.startswith("deployment/")]


def target_deployment(app_name: str, component: Optional[str]) -> str:
    deployments = _deployments(app_name)
    if not deployments:
        raise ToolError(f"Không tìm thấy Deployment nào của '{app_name}' trên K3s.")
    if component:
        name = f"{app_name}-{component}"
        if name not in deployments:
            raise ToolError(f"Không có Deployment '{name}'. Các Deployment hiện có: {', '.join(deployments)}")
        return name
    if app_name in deployments:
        return app_name
    if len(deployments) == 1:
        return deployments[0]
    raise ToolError(f"App có nhiều thành phần ({', '.join(deployments)}), hãy chỉ rõ component 'be' hoặc 'fe'.")


def _kubectl(args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "-n", builder_core.K8S_NAMESPACE, *args],
                          capture_output=True, text=True, timeout=timeout)


# ─────────────────────────── Kiểm tra HTTPS ───────────────────────────

def cert_days_left(domain: str, timeout: int = 10) -> Optional[float]:
    ctx = ssl.create_default_context()
    with socket.create_connection((domain, 443), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=domain) as tls:
            cert = tls.getpeercert()
    not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (not_after - datetime.now(timezone.utc)).total_seconds() / 86400


def probe_domain(domain: str, timeout: int = 10) -> Dict[str, Any]:
    """Gọi HTTPS tới domain: mã trả về, thời gian phản hồi, số ngày còn lại của chứng chỉ"""
    result: Dict[str, Any] = {"domain": domain, "ok": False}
    started = time.time()
    try:
        resp = requests.get(f"https://{domain}", timeout=timeout, allow_redirects=True)
        result["status_code"] = resp.status_code
        result["ok"] = resp.status_code < 500
    except requests.exceptions.SSLError as e:
        result["error"] = f"Lỗi chứng chỉ SSL: {str(e)[:150]}"
    except requests.RequestException as e:
        result["error"] = f"Không truy cập được: {type(e).__name__}"
    result["response_ms"] = int((time.time() - started) * 1000)
    try:
        result["cert_days_left"] = round(cert_days_left(domain, timeout), 1)
    except Exception:
        result["cert_days_left"] = None
    return result


# ─────────────────────────── Công cụ đọc ───────────────────────────

def tool_list_apps(user_id: str, args: Dict[str, Any]) -> str:
    apps = visible_apps(user_id)
    if not apps:
        return "Tài khoản chưa có ứng dụng nào."
    lines = [f"Có {len(apps)} ứng dụng:"]
    orphans = 0
    is_admin = user_directory.is_admin(user_id)
    users = user_directory.get_directory()["users"] if is_admin else {}
    for app in apps:
        domains = app_domains(app)
        line = f"- {app}" + (f" ({', '.join(domains)})" if domains else "")
        owners = user_directory.owners_of(app)
        # Chỉ admin thấy app không thuộc ai: còn cấu hình trên Vibe Core nhưng không còn project trên web
        if not owners:
            line += " ⚠️ không có chủ trên web"
            orphans += 1
        elif is_admin and user_id not in owners:
            # Admin thấy cả app của người khác: ghi rõ chủ để không nhầm với app của mình
            line += " · của " + ", ".join(f"@{(users.get(o) or {}).get('username', o)}" for o in owners)
        lines.append(line)
    if orphans:
        lines.append(f"\n{orphans} app không còn project trên web (cấu hình sót lại trên máy chủ). "
                     "Hermes không xóa được app; admin dọn trên web hoặc trên máy chủ.")
    return "\n".join(lines)


def summarize_workload(info: Dict[str, Any]) -> str:
    if not info.get("found"):
        return f"{info['workload']}: không tìm thấy ({info.get('error', '')})"
    lines = [f"{info['workload']}: {info.get('ready_replicas', 0)}/{info.get('replicas', 1)} Ready, revision {info.get('revision') or '-'}"]
    for pod in info.get("pods", []):
        states = []
        for c in pod.get("containers", []):
            s = f"{c.get('name')}={c.get('state')}"
            if c.get("reason"):
                s += f"({c['reason']})"
            if c.get("restarts"):
                s += f", restart {c['restarts']} lần"
            if c.get("last_terminated", {}).get("reason"):
                s += f", lần dừng trước: {c['last_terminated']['reason']}"
            states.append(s)
        lines.append(f"  • Pod {pod.get('name')} [{pod.get('phase')}] " + "; ".join(states))
    return "\n".join(lines)


def tool_app_status(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    workloads = hermes_verifier.discover_workloads(app)
    if not workloads:
        return f"Không có Deployment/StatefulSet nào của {app} trên K3s (app chưa triển khai hoặc đã bị xóa)."
    parts = [summarize_workload(hermes_verifier.collect_workload(w, with_logs=False)) for w in workloads]
    return _clip(f"Trạng thái {app}:\n" + "\n".join(parts))


def tool_runtime_logs(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    deployment = target_deployment(app, _component(args))
    try:
        lines = max(10, min(int(args.get("lines") or 80), 300))
    except (TypeError, ValueError):
        lines = 80
    res = _kubectl(["logs", f"deployment/{deployment}", "--all-containers", f"--tail={lines}"])
    out = res.stdout.strip()
    prev = _kubectl(["logs", f"deployment/{deployment}", "--all-containers", "--previous", f"--tail={lines}"])
    if prev.returncode == 0 and prev.stdout.strip():
        out = f"{out}\n[LOG LẦN CRASH TRƯỚC]\n{prev.stdout.strip()}"
    if not out:
        out = res.stderr.strip() or "Container chưa sinh log."
    return _clip(f"Log runtime {deployment} ({lines} dòng cuối):\n{out}")


def tool_build_log(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    tail = hermes_verifier.read_build_log_tail(app, max_lines=60)
    return _clip(f"Log build/triển khai gần nhất của {app}:\n{tail or 'Chưa có log build.'}")


def tool_k8s_events(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    events = hermes_verifier.collect_events(sorted(app_scope.workload_names(app)))
    return _clip(f"Sự kiện K3s liên quan {app}:\n{events or 'Không có sự kiện gần đây.'}")


def tool_health_report(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    report = hermes_verifier.load_report(app)
    if not report:
        return f"Chưa có báo cáo kiểm tra sau triển khai cho {app}."
    return _clip(hermes_verifier.format_report(report))


def tool_http_check(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    domains = app_domains(app)
    if not domains:
        return f"{app} chưa có tên miền trong cấu hình."
    lines = [f"Kiểm tra HTTPS {app}:"]
    for d in domains:
        r = probe_domain(d)
        status = f"HTTP {r['status_code']}" if "status_code" in r else r.get("error", "lỗi")
        cert = f", chứng chỉ còn {r['cert_days_left']} ngày" if r.get("cert_days_left") is not None else ""
        lines.append(f"- https://{d}: {status}, {r['response_ms']} ms{cert}")
    return "\n".join(lines)


def tool_deploy_history(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    deployment = target_deployment(app, _component(args))
    history = hermes_verifier.revision_history(app, deployment) or []
    if not history:
        return f"Chưa có lịch sử phiên bản cho {deployment}."
    labels = {"running": "đang chạy", "starting": "đang khởi động", "failed": "lỗi", "previous": "bản cũ"}
    lines = [f"Lịch sử phiên bản {deployment}:"]
    for h in history[:8]:
        mark = " ← hiện tại" if h.get("is_current") else ""
        lines.append(f"- revision {h['revision']} ({h.get('created_at', '')}): {labels.get(h['state'], h['state'])}, "
                     f"image {h.get('image', '')}{mark}")
    return "\n".join(lines)


# ─────────────────────────── Công cụ thay đổi hệ thống ───────────────────────────

def tool_restart_app(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    deployment = target_deployment(app, _component(args))
    res = _kubectl(["rollout", "restart", f"deployment/{deployment}"])
    if res.returncode != 0:
        raise ToolError(f"Khởi động lại thất bại: {res.stderr.strip()[:300]}")
    return f"Đã khởi động lại {deployment}. Pod mới sẽ Ready sau ít phút."


def tool_rollback_app(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    deployment = target_deployment(app, _component(args))
    cmd = ["rollout", "undo", f"deployment/{deployment}"]
    try:
        revision = int(args.get("revision") or 0)
    except (TypeError, ValueError):
        raise ToolError("revision phải là số nguyên.")
    if revision > 0:
        cmd.append(f"--to-revision={revision}")
    res = _kubectl(cmd)
    if res.returncode != 0:
        raise ToolError(f"Rollback thất bại: {res.stderr.strip()[:300]}")
    target = f"revision {revision}" if revision > 0 else "phiên bản liền trước"
    return f"Đã rollback {deployment} về {target}."


def tool_redeploy_app(user_id: str, args: Dict[str, Any]) -> str:
    app = check_access(user_id, args.get("app"))
    component = _component(args)
    cfg = cicd_core.get_active_app_config(app)
    branch = (args.get("branch") or cfg.get("branch") or "main").strip()
    threading.Thread(
        target=cicd_core.redeploy_app,
        kwargs={"app_name": app, "branch": branch, "pull_git": True, "target_component": component},
        daemon=True,
    ).start()
    return f"Đã bắt đầu build lại {app} từ nhánh {branch}. Hermes sẽ báo khi có kết quả kiểm tra."


# Các kiểu sửa lỗi được phép qua Telegram. Không có apply_manifest: YAML tùy ý quá rộng để duyệt
# qua một tin nhắn; mọi kiểu còn lại đều bị giới hạn trong tài nguyên của chính app (app_scope).
TELEGRAM_FIX_TYPES = ("set_env", "restart_deployment", "exec_pod", "edit_file", "rebuild")


def fix_allowed(action: Any) -> bool:
    return isinstance(action, dict) and action.get("action_type") in TELEGRAM_FIX_TYPES


def tool_apply_fix(user_id: str, args: Dict[str, Any]) -> str:
    """Thực thi một hành động khắc phục do bước chẩn đoán đề xuất (set_env, exec_pod, edit_file, rebuild...)"""
    from hermes_agent_router import RemediationAction, execute_remediation_action
    app = check_access(user_id, args.get("app"))
    action = args.get("action")
    if not isinstance(action, dict):
        raise ToolError("Thiếu nội dung hành động khắc phục.")
    if not fix_allowed(action):
        raise ToolError(f"Không cho phép thao tác {action.get('action_type')} qua Telegram.")
    try:
        parsed = RemediationAction(**action)
    except Exception as e:
        raise ToolError(f"Hành động không hợp lệ: {e}")
    result = execute_remediation_action(app, parsed)
    status = result.get("status", "")
    message = result.get("message") or json.dumps(result, ensure_ascii=False)[:500]
    if status == "error":
        raise ToolError(message)
    return _clip(message, 1500)


# ─────────────────────────── Chẩn đoán (đề xuất hành động) ───────────────────────────

def diagnose(user_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Thu thập ngữ cảnh và nhờ Hermes đề xuất hành động khắc phục (dùng lại logic của AI Debug).
    Trả về dict {diagnosis, root_cause, suggested_solution, actions[]}; actions chưa được thực thi."""
    from hermes_agent_router import collect_debug_context, query_hermes_diagnostics
    app = check_access(user_id, args.get("app"))
    component = _component(args) or "be"
    context = collect_debug_context(app, component)
    # Dùng key AI mặc định của user nếu có, không thì model của hệ thống
    engine = ai_credentials.default_engine(user_id) or "hermes"
    result = query_hermes_diagnostics(context, user_note=args.get("note"), ai_provider=engine, owner_user_id=user_id)
    result["app"] = app
    return result


# ─────────────────────────── Gói hosting và quyền ───────────────────────────

PERMISSIONS_TEXT = (
    "✅ Hermes ĐƯỢC làm (chỉ với ứng dụng của bạn):\n"
    "• Xem trạng thái, log, sự kiện K3s, lịch sử phiên bản, kiểm tra HTTPS\n"
    "• Chẩn đoán lỗi và đề xuất cách sửa\n"
    "• Restart, rollback, build lại, sửa biến môi trường / file cấu hình: chỉ sau khi bạn bấm ✅ Duyệt\n"
    "\n"
    "⛔ Hermes KHÔNG được làm:\n"
    "• Xem hay thao tác ứng dụng của người dùng khác\n"
    "• Tạo, xóa ứng dụng, đổi tên miền (làm trên web)\n"
    "• Nâng CPU/RAM/dung lượng vượt mức đã phân bổ cho app\n"
    "• Chạy lệnh lên máy chủ, cụm K3s hay hệ thống dùng chung"
)


def package_text(user_id: str) -> str:
    """Gói hosting, mức đã dùng, AI đang dùng và phạm vi quyền của Hermes với user"""
    user = user_directory.get_user(user_id) or {}
    pkg = user.get("package") or {}
    owned = len(user_directory.owned_apps(user_id))
    lines = [f"📦 Gói hosting: {pkg.get('name') or 'chưa xác định'}"]
    if pkg.get("description"):
        lines.append(pkg["description"])
    if pkg.get("unlimited") or user.get("role") == "admin":
        lines.append("• Tài khoản admin: không giới hạn tài nguyên")
    elif pkg:
        lines.append(f"• Project: {owned}/{pkg.get('maxProjects')}")
        lines.append(f"• Tổng CPU của gói: {pkg.get('cpu')}")
        lines.append(f"• Tổng RAM của gói: {pkg.get('memory')}")
        lines.append(f"• Database: {('tổng ' + str(pkg.get('dbStorage'))) if pkg.get('allowDatabase') else 'không có trong gói'}")
        lines.append("  (chia cho các project khi tạo; đổi mức của từng project ở tab Cài đặt trên web)")
    else:
        lines.append(f"• Project: {owned} (chưa tải được giới hạn của gói, cập nhật web để xem)")
    engine = ai_credentials.default_engine(user_id)
    if engine:
        cred = next((c for c in ai_credentials.list_for(user_id) if c["engine"] == engine), None)
        ai_line = f"key riêng của bạn – {cred['provider_name']} {cred['model']}" if cred else "key riêng của bạn"
    else:
        ai_line = "model có sẵn của hệ thống (gắn key riêng ở trang hồ sơ trên web)"
    lines.append(f"🤖 AI của Hermes: {ai_line}")
    lines.append("")
    lines.append(PERMISSIONS_TEXT)
    return "\n".join(lines)


def tool_my_package(user_id: str, args: Dict[str, Any]) -> str:
    return package_text(user_id)


# ─────────────────────────── AI Debug chế độ Agent (qua Telegram) ───────────────────────────

AGENT_ENGINES = {
    "hydrogen": "hydrogen", "gemini-4-hydrogen": "hydrogen", "gemini 4 hydrogen": "hydrogen", "gemini4": "hydrogen",
    "agy": "agy", "antigravity": "agy", "gemini": "agy", "gemini-flash": "agy", "flash": "agy",
    "claude": "claude", "claude-code": "claude", "claude code": "claude", "anthropic": "claude",
}
AGENT_ENGINE_NAMES = {"hydrogen": "Gemini 4 Hydrogen", "agy": "Google Antigravity (Gemini Flash)", "claude": "Claude Code"}
DEFAULT_AGENT_ENGINE = "hydrogen"


def normalize_agent_engine(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return DEFAULT_AGENT_ENGINE
    if key not in AGENT_ENGINES:
        raise ToolError("Model cho AI Debug chỉ có: hydrogen (Gemini 4 Hydrogen), agy (Antigravity), claude (Claude Code).")
    return AGENT_ENGINES[key]


def start_ai_debug(user_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Mở phiên AI Debug chế độ Agent cho app của user (chạy nền): model đọc/sửa mã nguồn và file K3s trên BẢN SAO,
    chỉ áp dụng khi người dùng bấm duyệt. Trả về phiên (id, app, engine)"""
    import ai_agent_session as agent
    from hermes_agent_router import collect_debug_context, resolve_workspace
    app = check_access(user_id, args.get("app"))
    request = str(args.get("request") or args.get("note") or "").strip()
    if not request:
        raise ToolError("Thiếu mô tả việc cần AI Debug kiểm tra/sửa.")
    engine = normalize_agent_engine(args.get("engine"))
    component = _component(args) or "be"
    workspace = resolve_workspace(app, component)
    if not workspace:
        raise ToolError(f"Không tìm thấy mã nguồn của {app} trên máy chủ.")
    try:
        return agent.start_session(app, component, request, engine, user_id, workspace,
                                   collect_debug_context(app, component), app_domains(app))
    except agent.AgentError as e:
        raise ToolError(str(e))


def tool_apply_agent(user_id: str, args: Dict[str, Any]) -> str:
    """Áp dụng các file phiên AI Debug đã sửa (chỉ chạy sau khi người dùng bấm Duyệt)"""
    import ai_agent_session as agent
    from hermes_agent_router import _agent_execute
    app = check_access(user_id, args.get("app"))
    try:
        session = agent.load_session(str(args.get("session") or ""))
        if session.get("user_id") not in (None, user_id) and not user_directory.is_admin(user_id):
            raise ToolError("Phiên AI Debug này không phải của bạn.")
        result = agent.apply_session(session["id"], app, args.get("paths"), True, _agent_execute)
    except agent.AgentError as e:
        raise ToolError(str(e))
    lines = [f"Đã áp dụng thay đổi AI Debug cho {app}:"]
    for r in result["results"]:
        mark = "✅" if r.get("status") == "success" else "⏭️"
        extra = ""
        if r.get("apply"):
            extra = " · apply K3s " + ("thành công" if r["apply"].get("status") == "success" else f"lỗi: {r['apply'].get('message')}")
        lines.append(f"{mark} {r['path']}{extra}" + (f" ({r.get('message')})" if r.get("status") != "success" else ""))
    if result.get("rebuild"):
        rb = result["rebuild"]
        lines.append("🔨 Build lại: " + ("thành công" if rb.get("status") == "success" else f"lỗi: {rb.get('message')}"))
    return "\n".join(lines)


# ─────────────────────────── Bảng đăng ký công cụ ───────────────────────────

Tool = Dict[str, Any]

TOOLS: Dict[str, Tool] = {
    "list_apps": {"fn": tool_list_apps, "mutating": False, "args": "{}",
                  "desc": "Liệt kê các ứng dụng người dùng được quản lý."},
    "my_package": {"fn": tool_my_package, "mutating": False, "args": "{}",
                   "desc": "Gói hosting của người dùng (số project, CPU/RAM, database), AI đang dùng và việc Hermes được/không được làm."},
    "app_status": {"fn": tool_app_status, "mutating": False, "args": '{"app": "tên app"}',
                   "desc": "Trạng thái Deployment/Pod trên K3s: Ready, restart, lý do lỗi."},
    "runtime_logs": {"fn": tool_runtime_logs, "mutating": False,
                     "args": '{"app": "tên app", "component": "be|fe (tùy chọn)", "lines": 80}',
                     "desc": "Log runtime của container, kèm log lần crash trước nếu có."},
    "build_log": {"fn": tool_build_log, "mutating": False, "args": '{"app": "tên app"}',
                  "desc": "Đoạn cuối log build/triển khai gần nhất."},
    "k8s_events": {"fn": tool_k8s_events, "mutating": False, "args": '{"app": "tên app"}',
                   "desc": "Sự kiện K3s gần đây (kéo image lỗi, thiếu tài nguyên, probe thất bại...)."},
    "health_report": {"fn": tool_health_report, "mutating": False, "args": '{"app": "tên app"}',
                      "desc": "Báo cáo Hermes Verifier của lần triển khai gần nhất."},
    "http_check": {"fn": tool_http_check, "mutating": False, "args": '{"app": "tên app"}',
                   "desc": "Gọi HTTPS tới tên miền của app: mã trả về, thời gian phản hồi, hạn chứng chỉ."},
    "deploy_history": {"fn": tool_deploy_history, "mutating": False,
                       "args": '{"app": "tên app", "component": "be|fe (tùy chọn)"}',
                       "desc": "Lịch sử phiên bản (revision) và trạng thái từng bản."},
    "ai_debug": {"fn": None, "mutating": False,
                 "args": '{"app": "tên app", "request": "việc cần kiểm tra/sửa", "engine": "hydrogen|agy|claude", "component": "be|fe (tùy chọn)"}',
                 "desc": "Chạy chức năng AI Debug (chế độ Agent) của hosting: model đọc mã nguồn, file K3s của app trên bản sao, "
                         "tìm lỗi và sửa; xong sẽ gửi kết quả kèm nút Áp dụng. engine: hydrogen = Gemini 4 Hydrogen (mặc định), "
                         "agy = Antigravity Gemini Flash, claude = Claude Code. Mất khoảng 1–10 phút."},
    "diagnose": {"fn": None, "mutating": False, "args": '{"app": "tên app", "component": "be|fe (tùy chọn)", "note": "mô tả lỗi"}',
                 "desc": "Chẩn đoán sâu và đề xuất hành động khắc phục; người dùng sẽ được hỏi duyệt từng hành động."},
    "restart_app": {"fn": tool_restart_app, "mutating": True,
                    "args": '{"app": "tên app", "component": "be|fe (tùy chọn)"}',
                    "desc": "Khởi động lại Pod (rollout restart). CẦN NGƯỜI DÙNG DUYỆT."},
    "rollback_app": {"fn": tool_rollback_app, "mutating": True,
                     "args": '{"app": "tên app", "component": "be|fe (tùy chọn)", "revision": 0}',
                     "desc": "Quay về phiên bản trước (revision 0 = bản liền trước). CẦN NGƯỜI DÙNG DUYỆT."},
    "redeploy_app": {"fn": tool_redeploy_app, "mutating": True,
                     "args": '{"app": "tên app", "component": "be|fe (tùy chọn)", "branch": "main"}',
                     "desc": "Kéo mã mới từ GitHub và build lại. CẦN NGƯỜI DÙNG DUYỆT."},
}

# apply_fix không cho Hermes gọi trực tiếp: chỉ sinh ra từ kết quả diagnose
INTERNAL_TOOLS: Dict[str, Tool] = {
    "apply_fix": {"fn": tool_apply_fix, "mutating": True, "args": "", "desc": ""},
    "apply_agent": {"fn": tool_apply_agent, "mutating": True, "args": "", "desc": ""},
}


def get_tool(name: str, include_internal: bool = False) -> Optional[Tool]:
    if name in TOOLS:
        return TOOLS[name]
    if include_internal:
        return INTERNAL_TOOLS.get(name)
    return None


def run_tool(user_id: str, name: str, args: Dict[str, Any], include_internal: bool = False) -> str:
    tool = get_tool(name, include_internal)
    if not tool or not tool.get("fn"):
        raise ToolError(f"Không có công cụ '{name}'.")
    fn: Callable[[str, Dict[str, Any]], str] = tool["fn"]
    return fn(user_id, args or {})


def describe_tools() -> str:
    return "\n".join(f"- {name} {t['args']}: {t['desc']}" for name, t in TOOLS.items())


_SECRET_KEY = re.compile(r"PASS|SECRET|TOKEN|KEY|PRIVATE|CREDENTIAL", re.IGNORECASE)


def _mask_env_value(key: str, value: Any) -> str:
    """Hiện giá trị để người duyệt biết mình duyệt gì, nhưng che giá trị bí mật"""
    value = str(value)
    if _SECRET_KEY.search(key) and value:
        return f"(ẩn, {len(value)} ký tự)"
    return redact_secrets(value)[:200]


def describe_action(name: str, args: Dict[str, Any]) -> str:
    """Mô tả ngắn hành động để hiện trong tin hỏi duyệt"""
    app = args.get("app", "?")
    comp = f" ({args['component']})" if args.get("component") else ""
    if name == "restart_app":
        return f"Khởi động lại {app}{comp}"
    if name == "rollback_app":
        rev = int(args.get("revision") or 0) if str(args.get("revision") or "0").isdigit() else 0
        return f"Rollback {app}{comp} về " + (f"revision {rev}" if rev else "phiên bản liền trước")
    if name == "redeploy_app":
        return f"Build lại {app}{comp} từ nhánh {args.get('branch') or 'main'}"
    if name == "apply_agent":
        files = args.get("paths") or []
        listed = ", ".join(files[:6]) + (f" và {len(files) - 6} file khác" if len(files) > 6 else "")
        return f"Áp dụng {len(files)} file AI Debug đã sửa cho {app}: {listed} (ghi mã, apply K3s, build lại nếu sửa code)"
    if name == "apply_fix":
        action = args.get("action") or {}
        kind = action.get("action_type", "")
        lines = [f"{app}: {kind} – {action.get('description') or ''}".rstrip(" –")]
        if action.get("target"):
            lines.append(f"Đối tượng: {action['target']}")
        if kind == "set_env" and action.get("env_vars"):
            lines.append("Biến môi trường:")
            lines += [f"  {k}={_mask_env_value(k, v)}" for k, v in action["env_vars"].items()]
        elif kind == "exec_pod" and action.get("command"):
            lines.append(f"Lệnh chạy trong container của app:\n  {action['command'][:600]}")
        elif kind == "edit_file":
            path = action.get("path") or action.get("file_path") or "?"
            content = action.get("content") if action.get("content") is not None else action.get("diff") or ""
            lines.append(f"Ghi đè tệp {path} ({len(content.splitlines())} dòng); sau đó cần build lại.")
        return "\n".join(lines)
    return f"{name} {json.dumps(args, ensure_ascii=False)[:200]}"
