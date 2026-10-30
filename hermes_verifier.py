"""
Hermes Verifier - AI Agent ở bước cuối của mọi pipeline Deploy / CI-CD.

Agy / Claude Code chịu trách nhiệm viết cấu hình (Nixpacks, K3s). Hermes chỉ đứng ngoài QUAN SÁT:
- Chờ các workload K3s (Deployment / StatefulSet) rollout xong
- Thu thập trạng thái Pod, Event, log container và log build
- Nhờ Hermes đánh giá: deploy đã thành công chưa, nếu lỗi thì lỗi ở tầng nào (code, cấu hình K3s, build)
- In báo cáo vào log build cho người dùng và lưu bản JSON tại {LOGS_DIR}/{app_name}.health.json

Hermes không thực thi bất kỳ thay đổi nào. Kết luận DONE / ERROR dựa trên trạng thái thực tế của K3s,
Hermes chỉ giải thích nguyên nhân và gợi ý cách sửa.
"""

import json
import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

import builder_core
from builder_core import K8S_NAMESPACE, run_hermes_json
from security import redact_secrets

ROLLOUT_TIMEOUT = int(os.getenv("VERIFY_ROLLOUT_TIMEOUT", "180"))
HERMES_VERIFY_ENABLED = os.getenv("HERMES_VERIFY", "1") != "0"
HERMES_VERIFY_TIMEOUT = int(os.getenv("HERMES_VERIFY_TIMEOUT", "45"))
HERMES_VERIFY_MAX_MODELS = 3

STATUS_LABELS = {
    "healthy": "✅ Hoạt động ổn định",
    "starting": "⏳ Pod chưa sẵn sàng",
    "pipeline_error": "❌ Lỗi trong quá trình build / triển khai",
    "build_error": "❌ Lỗi build mã nguồn",
    "code_error": "❌ Lỗi mã nguồn (container crash khi chạy)",
    "k8s_config_error": "❌ Lỗi cấu hình K3s (Pod không thể khởi động)",
    "unknown": "❔ Chưa xác định",
}

# Lý do waiting cho thấy K3s không tạo/chạy được container (image, secret, env, volume...)
K8S_CONFIG_REASONS = {
    "ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull",
    "CreateContainerConfigError", "CreateContainerError",
}
# Container đã chạy nhưng tự thoát => thường do mã nguồn hoặc biến môi trường của ứng dụng
APP_CRASH_REASONS = {"CrashLoopBackOff", "Error", "OOMKilled", "RunContainerError"}


def _kubectl(args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = ["kubectl", *args, "-n", K8S_NAMESPACE]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def report_path(app_name: str) -> str:
    return os.path.join(builder_core.LOGS_DIR, f"{app_name}.health.json")


def load_report(app_name: str) -> Optional[Dict[str, Any]]:
    try:
        with open(report_path(app_name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def discover_workloads(app_name: str) -> List[str]:
    """Tìm các workload của app đang có trên K3s (dùng khi kiểm tra lại theo yêu cầu)"""
    candidates = {app_name, f"{app_name}-be", f"{app_name}-fe", f"{app_name}-db"}
    res = _kubectl(["get", "deployment,statefulset", "-o", "name"])
    found = []
    for line in res.stdout.splitlines():
        kind, _, name = line.strip().partition("/")
        if name in candidates:
            found.append(f"{kind.split('.')[0]}/{name}")
    # StatefulSet (DB) trước để chờ DB sẵn sàng rồi mới tới app
    return sorted(found, key=lambda w: not w.startswith("statefulset/"))


def wait_for_rollout(workload: str) -> Dict[str, Any]:
    res = _kubectl(["rollout", "status", workload, f"--timeout={ROLLOUT_TIMEOUT}s"], timeout=ROLLOUT_TIMEOUT + 15)
    return {"ready": res.returncode == 0, "message": (res.stdout or res.stderr).strip()[-300:]}


def _container_states(pod: Dict[str, Any]) -> List[Dict[str, Any]]:
    states = []
    for cs in pod.get("status", {}).get("containerStatuses", []) or []:
        state = cs.get("state", {}) or {}
        last = (cs.get("lastState", {}) or {}).get("terminated") or {}
        entry = {
            "name": cs.get("name"),
            "ready": bool(cs.get("ready")),
            "restarts": cs.get("restartCount", 0),
        }
        if "waiting" in state:
            entry["state"] = "waiting"
            entry["reason"] = state["waiting"].get("reason", "")
            entry["message"] = (state["waiting"].get("message") or "")[:300]
        elif "terminated" in state:
            entry["state"] = "terminated"
            entry["reason"] = state["terminated"].get("reason", "")
            entry["exit_code"] = state["terminated"].get("exitCode")
        else:
            entry["state"] = "running"
        if last:
            entry["last_terminated"] = {"reason": last.get("reason"), "exit_code": last.get("exitCode")}
        states.append(entry)
    return states


def collect_workload(workload: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {"workload": workload, "found": False}
    res = _kubectl(["get", workload, "-o", "json"])
    if res.returncode != 0:
        info["error"] = res.stderr.strip()[:300]
        return info
    try:
        obj = json.loads(res.stdout)
    except Exception:
        info["error"] = "Không đọc được JSON từ kubectl"
        return info

    info["found"] = True
    info["replicas"] = obj.get("spec", {}).get("replicas", 1)
    info["ready_replicas"] = obj.get("status", {}).get("readyReplicas", 0) or 0
    labels = obj.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {}
    selector = ",".join(f"{k}={v}" for k, v in labels.items())

    pods = []
    if selector:
        pod_res = _kubectl(["get", "pods", "-l", selector, "-o", "json"])
        try:
            items = json.loads(pod_res.stdout).get("items", []) if pod_res.returncode == 0 else []
        except Exception:
            items = []
        for pod in items:
            pods.append({
                "name": pod.get("metadata", {}).get("name"),
                "phase": pod.get("status", {}).get("phase"),
                "conditions": [
                    {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"), "message": (c.get("message") or "")[:200]}
                    for c in pod.get("status", {}).get("conditions", []) or []
                    if c.get("status") != "True"
                ],
                "containers": _container_states(pod),
            })
    info["pods"] = pods

    # Log của Pod có vấn đề (hoặc Pod đầu tiên) để Hermes đọc được stack trace
    target = next((p for p in pods if any(not c["ready"] for c in p["containers"])), pods[0] if pods else None)
    if target:
        logs = _kubectl(["logs", target["name"], "--all-containers", "--tail=60"]).stdout
        if any(c["restarts"] for c in target["containers"]):
            prev = _kubectl(["logs", target["name"], "--all-containers", "--previous", "--tail=60"]).stdout
            if prev:
                logs = f"{logs}\n[LOG LẦN CRASH TRƯỚC]\n{prev}"
        info["pod_logs"] = redact_secrets(logs.strip())[-4000:]
    return info


def collect_events(names: List[str]) -> str:
    res = _kubectl(["get", "events", "--sort-by=.lastTimestamp"])
    lines = [line for line in res.stdout.splitlines() if any(n in line for n in names)]
    return "\n".join(lines[-20:])


def read_build_log_tail(app_name: str, max_lines: int = 80) -> str:
    try:
        with open(os.path.join(builder_core.LOGS_DIR, f"{app_name}.log"), "r", encoding="utf-8") as f:
            return "".join(f.readlines()[-max_lines:])
    except Exception:
        return ""


def classify(snapshot: List[Dict[str, Any]], error: Optional[str]) -> str:
    """Phân loại sơ bộ không cần AI, dùng làm kết luận dự phòng khi Hermes không phản hồi"""
    if error:
        return "pipeline_error"
    if not snapshot:
        return "unknown"
    if any(not w["found"] for w in snapshot):
        return "k8s_config_error"
    reasons = set()
    unscheduled = False
    for w in snapshot:
        for p in w.get("pods", []):
            for c in p["containers"]:
                if c.get("reason"):
                    reasons.add(c["reason"])
                if (c.get("last_terminated") or {}).get("reason"):
                    reasons.add(c["last_terminated"]["reason"])
            if p["phase"] == "Pending" and not p["containers"]:
                unscheduled = True
    if reasons & K8S_CONFIG_REASONS or unscheduled:
        return "k8s_config_error"
    if reasons & APP_CRASH_REASONS:
        return "code_error"
    if all(w["ready_replicas"] >= (w["replicas"] or 1) for w in snapshot):
        return "healthy"
    return "starting"


def ask_hermes(app_name: str, snapshot: List[Dict[str, Any]], events: str, build_log: str,
               error: Optional[str], ready: bool, fallback_status: str) -> Optional[Dict[str, Any]]:
    prompt = f"""
Bạn là Hermes - AI Agent kiểm tra tổng thể (read-only) của nền tảng Vibe Hosting (K3s + Nixpacks).
Cấu hình đã được AI khác viết và áp dụng. Nhiệm vụ của bạn CHỈ LÀ QUAN SÁT VÀ ĐÁNH GIÁ, không đề xuất lệnh để hệ thống tự chạy.

Hãy trả lời: ứng dụng "{app_name}" đã triển khai thành công chưa? Nếu chưa, lỗi nằm ở tầng nào:
- "build_error": lỗi khi build image (cài thư viện, compile, nixpacks)
- "code_error": container đã chạy nhưng crash do mã nguồn / thiếu biến môi trường của ứng dụng
- "k8s_config_error": cấu hình K3s khiến Pod không start (image, port, probe, secret, volume, ingress, tài nguyên)
- "pipeline_error": lỗi hạ tầng của pipeline (git clone, docker push, kubectl apply...)
- "healthy": mọi Pod đã Ready

[KẾT QUẢ KIỂM TRA TỰ ĐỘNG]
Lỗi pipeline: {error or "Không có"}
Rollout sẵn sàng: {ready}
Phân loại sơ bộ: {fallback_status}

[TRẠNG THÁI WORKLOAD / POD]
{json.dumps(snapshot, ensure_ascii=False, indent=1)[:9000]}

[SỰ KIỆN K3S GẦN NHẤT]
{events or "Không có"}

[CUỐI LOG BUILD]
{build_log[-4000:] or "Không có"}

BẮT BUỘC chỉ trả về JSON thuần, viết bằng tiếng Việt:
{{
  "status": "healthy | build_error | code_error | k8s_config_error | pipeline_error",
  "summary": "Kết luận 1-2 câu cho người dùng",
  "evidence": ["Dòng log / trạng thái cụ thể chứng minh kết luận"],
  "suggestions": ["Việc người dùng nên làm để sửa (để trống nếu healthy)"]
}}
"""
    try:
        return run_hermes_json(prompt, app_name="system", timeout=HERMES_VERIFY_TIMEOUT, max_models=HERMES_VERIFY_MAX_MODELS)
    except Exception as e:
        print(f"⚠️ [HERMES VERIFIER] Hermes không phản hồi: {e}", flush=True)
        return None


def verify_deployment(app_name: str, workloads: List[str], error: Optional[str] = None,
                      log: Optional[Callable[[str, str], None]] = None) -> Dict[str, Any]:
    """
    Kiểm tra tổng thể sau khi pipeline kết thúc. Nếu truyền `log`, báo cáo được in vào log build.
    Không bao giờ raise: lỗi khi kiểm tra chỉ làm báo cáo kém chi tiết hơn.
    """
    started = time.time()
    if log:
        log(app_name, "\n🩺 [HERMES VERIFIER] Đang kiểm tra tổng thể trạng thái ứng dụng trên K3s...")

    rollout = {}
    if not error:
        for w in workloads:
            rollout[w] = wait_for_rollout(w)
    ready = bool(workloads) and not error and all(r["ready"] for r in rollout.values())

    snapshot = [collect_workload(w) for w in workloads]
    events = collect_events([w.split("/")[-1] for w in workloads] or [app_name])
    build_log = read_build_log_tail(app_name)
    fallback_status = classify(snapshot, error)

    ai = ask_hermes(app_name, snapshot, events, build_log, error, ready, fallback_status) if HERMES_VERIFY_ENABLED else None

    # Trạng thái thực tế của K3s là căn cứ cuối cùng, Hermes chỉ được tinh chỉnh tầng lỗi khi chưa Ready
    status = fallback_status
    if ready:
        status = "healthy"
    elif ai and ai.get("status") in STATUS_LABELS and ai.get("status") != "healthy":
        status = ai["status"]
    elif status == "healthy":
        status = "starting"

    report = {
        "app_name": app_name,
        "ready": ready,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "summary": (ai or {}).get("summary") or STATUS_LABELS[status],
        "evidence": [str(x) for x in (ai or {}).get("evidence", []) or []][:6],
        "suggestions": [str(x) for x in (ai or {}).get("suggestions", []) or []][:6],
        "analyzed_by": "hermes" if ai else "rule-based",
        "pipeline_error": redact_secrets(error) if error else None,
        "workloads": [
            {
                "workload": w["workload"],
                "found": w["found"],
                "ready": f"{w.get('ready_replicas', 0)}/{w.get('replicas', 0)}" if w["found"] else "không tồn tại",
                "rollout": rollout.get(w["workload"], {}).get("message", ""),
                "pods": w.get("pods", []),
            }
            for w in snapshot
        ],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_seconds": round(time.time() - started, 1),
    }

    try:
        with open(report_path(app_name), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

    if log:
        log(app_name, format_report(report))
    return report


def format_report(report: Dict[str, Any]) -> str:
    lines = [
        "🩺 [HERMES VERIFIER] Kết quả kiểm tra tổng thể",
        f"   Trạng thái : {report['status_label']}",
    ]
    for w in report["workloads"]:
        lines.append(f"   Workload   : {w['workload']} ({w['ready']} Ready)")
    lines.append(f"   Đánh giá   : {report['summary']}")
    for e in report["evidence"]:
        lines.append(f"   Bằng chứng : - {e}")
    for s in report["suggestions"]:
        lines.append(f"   Gợi ý      : - {s}")
    lines.append(f"   (Phân tích bởi: {report['analyzed_by']}, {report['duration_seconds']}s)")
    return "\n".join(lines)


def finalize_pipeline(app_name: str, workloads: List[str], error: Optional[str] = None,
                      success_lines: Optional[List[str]] = None,
                      log: Optional[Callable[[str, str], None]] = None) -> bool:
    """
    Bước cuối của mọi pipeline: chạy Hermes Verifier rồi mới ghi dấu kết thúc cho Frontend.
    Dòng hoàn tất (success_lines) + DONE chỉ được ghi khi Pod thực sự Ready, ngược lại ghi ERROR.
    """
    log = log or builder_core.write_log
    try:
        ok = verify_deployment(app_name, workloads, error=error, log=log)["ready"]
    except Exception as e:
        log(app_name, f"⚠️ [HERMES VERIFIER] Không thể kiểm tra trạng thái: {e}")
        ok = error is None

    if ok:
        for line in success_lines or []:
            log(app_name, line)
        log(app_name, "DONE")
    else:
        log(app_name, f"=== [THẤT BẠI] {app_name} chưa chạy ổn định, xem báo cáo Hermes ở trên ===")
        log(app_name, "ERROR")
    return ok
