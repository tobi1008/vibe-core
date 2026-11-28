"""
Giới hạn tài nguyên theo gói hosting (package) của chủ app.

Gói do admin cấu hình trên web (frontend). Trước mỗi lần triển khai, frontend gửi giới hạn của gói
cho Vibe Core qua POST /api/apps/{app}/resources (xác thực bằng VIBE_API_KEY), Vibe Core lưu vào
app_resources.json (file riêng, để app chưa triển khai không thành "app ma" trong apps_config.json):
    {"cpu": "500m", "memory": "512Mi", "db_storage": "1Gi", "allow_database": true}
Không dùng giới hạn trình duyệt gửi kèm khi upload file nén, vì trình duyệt gọi thẳng Vibe Core.

Manifest K3s do AI sinh ra nên không tin giá trị resources AI viết: ngay trước mỗi `kubectl apply -f`
(builder_core.run_cmd), enforce_file ghi đè limits/requests của mọi container trong Deployment /
StatefulSet và dung lượng PVC của app. App chưa có cấu hình resources (app cũ, của admin) không bị đổi;
LimitRange của namespace (ensure_limit_range) là lưới an toàn cho container không khai báo limits.
"""

import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

import yaml

from user_directory import base_app_name

RESOURCES_FILE = "/opt/vibe-hosting/app_resources.json"

_CPU = re.compile(r"^(\d{1,6})m$")
_MEM = re.compile(r"^(\d{1,7})(Mi|Gi)$")

# Lưới an toàn cho container không khai báo limits (kể cả app cũ chưa có gói)
DEFAULT_LIMIT_CPU = os.getenv("NAMESPACE_DEFAULT_CPU", "1000m")
DEFAULT_LIMIT_MEMORY = os.getenv("NAMESPACE_DEFAULT_MEMORY", "1Gi")
LIMIT_RANGE_NAME = "vibe-default-limits"


class LimitsError(ValueError):
    pass


# ─────────────────────────── Đơn vị ───────────────────────────

def cpu_millis(value: str) -> int:
    m = _CPU.match(str(value or ""))
    if not m:
        raise LimitsError(f"CPU '{value}' không hợp lệ (vd 500m).")
    return int(m.group(1))


def memory_mi(value: str) -> int:
    m = _MEM.match(str(value or ""))
    if not m:
        raise LimitsError(f"Dung lượng '{value}' không hợp lệ (vd 512Mi, 1Gi).")
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


def _parse_quantity_mi(value: Any) -> Optional[int]:
    """Đọc quantity K8s bất kỳ (512Mi, 1Gi, 1G, 500M, 1073741824) ra MiB; không đọc được trả None"""
    m = re.match(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$", str(value or "").strip())
    if not m:
        return None
    n = float(m.group(1))
    factor = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 ** 2,
              "K": 1e3 / 1024 ** 2, "M": 1e6 / 1024 ** 2, "G": 1e9 / 1024 ** 2, "T": 1e12 / 1024 ** 2,
              None: 1 / 1024 ** 2}[m.group(2)]
    return int(n * factor)


def _parse_cpu_millis(value: Any) -> Optional[int]:
    s = str(value or "").strip()
    if s.endswith("m") and s[:-1].isdigit():
        return int(s[:-1])
    try:
        return int(float(s) * 1000)
    except ValueError:
        return None


def normalize(resources: Dict[str, Any]) -> Dict[str, Any]:
    """Kiểm tra và chuẩn hóa giới hạn nhận từ frontend"""
    if not isinstance(resources, dict):
        raise LimitsError("resources phải là object.")
    cpu = str(resources.get("cpu", "")).strip()
    memory = str(resources.get("memory", "")).strip()
    db_storage = str(resources.get("db_storage", "1Gi")).strip()
    if not 10 <= cpu_millis(cpu) <= 64000:
        raise LimitsError("CPU phải trong khoảng 10m – 64000m.")
    if not 32 <= memory_mi(memory) <= 256 * 1024:
        raise LimitsError("RAM phải trong khoảng 32Mi – 256Gi.")
    if not 1024 <= memory_mi(db_storage) <= 1024 * 1024:
        raise LimitsError("Dung lượng database phải trong khoảng 1Gi – 1024Gi.")
    return {"cpu": cpu, "memory": memory, "db_storage": db_storage,
            "allow_database": bool(resources.get("allow_database", True))}


def requests_for(limits: Dict[str, Any]) -> Dict[str, str]:
    """requests = 1/4 limits (tối thiểu 10m / 32Mi) để nhiều app chia sẻ node"""
    return {"cpu": f"{max(10, cpu_millis(limits['cpu']) // 4)}m",
            "memory": f"{max(32, memory_mi(limits['memory']) // 4)}Mi"}


# ─────────────────────────── Lưu cấu hình ───────────────────────────

def _load_all() -> Dict[str, Any]:
    if not os.path.exists(RESOURCES_FILE):
        return {}
    with open(RESOURCES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_all(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(RESOURCES_FILE), exist_ok=True)
    with open(RESOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_resources(app_name: str) -> Optional[Dict[str, Any]]:
    return _load_all().get(base_app_name(app_name))


def save_resources(app_name: str, resources: Dict[str, Any]) -> Dict[str, Any]:
    clean = normalize(resources)
    data = _load_all()
    data[base_app_name(app_name)] = clean
    _save_all(data)
    return clean


def forget(app_name: str) -> None:
    data = _load_all()
    if data.pop(base_app_name(app_name), None) is not None:
        _save_all(data)


def database_allowed(app_name: str) -> bool:
    res = get_resources(app_name)
    return res is None or res.get("allow_database", True) is not False


def wants_database(db_type: Optional[str]) -> bool:
    return bool(db_type) and str(db_type).strip().lower() not in ("none", "no", "false", "")


# ─────────────────────────── Ghi đè manifest ───────────────────────────

def _existing_pvc_size(name: str) -> Optional[str]:
    """PVC đã tồn tại thì giữ nguyên dung lượng: local-path không hỗ trợ đổi kích thước,
    apply dung lượng khác sẽ làm lệnh apply lỗi"""
    try:
        res = subprocess.run(["kubectl", "get", "pvc", name, "-o", "jsonpath={.spec.resources.requests.storage}"],
                             capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _apply_container_limits(containers: List[Dict[str, Any]], limits: Dict[str, Any]) -> None:
    req = requests_for(limits)
    for c in containers or []:
        if not isinstance(c, dict):
            continue
        c["resources"] = {"limits": {"cpu": limits["cpu"], "memory": limits["memory"]}, "requests": req}


def enforce_docs(app_name: str, docs: List[Any], limits: Dict[str, Any]) -> bool:
    """Ghi đè resources trong các document YAML; trả về True nếu có thay đổi"""
    changed = False
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        spec = doc.get("spec") or {}
        if kind in ("Deployment", "StatefulSet"):
            pod = ((spec.get("template") or {}).get("spec")) or {}
            _apply_container_limits(pod.get("containers"), limits)
            _apply_container_limits(pod.get("initContainers"), limits)
            changed = True
        elif kind == "PersistentVolumeClaim":
            name = (doc.get("metadata") or {}).get("name") or ""
            size = _existing_pvc_size(name) or limits["db_storage"]
            spec.setdefault("resources", {}).setdefault("requests", {})["storage"] = size
            doc["spec"] = spec
            changed = True
    return changed


def enforce_text(app_name: str, manifest_yaml: str) -> str:
    limits = get_resources(app_name)
    if not limits:
        return manifest_yaml
    docs = [d for d in yaml.safe_load_all(manifest_yaml) if d is not None]
    if not enforce_docs(app_name, docs, limits):
        return manifest_yaml
    return yaml.safe_dump_all(docs, sort_keys=False, allow_unicode=True)


def enforce_file(app_name: str, path: str) -> bool:
    """Ghi đè giới hạn trong file manifest trước khi apply. Lỗi đọc YAML thì để nguyên (LimitRange vẫn chặn)"""
    if not get_resources(app_name) or not os.path.isfile(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()
    try:
        updated = enforce_text(app_name, original)
    except yaml.YAMLError:
        return False
    if updated == original:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    return True


def apply_target(cmd: List[str], cwd: Optional[str]) -> Optional[str]:
    """Đường dẫn file của lệnh `kubectl apply -f <file>`, None nếu không phải"""
    if len(cmd) < 4 or cmd[0] != "kubectl" or cmd[1] != "apply" or "-f" not in cmd:
        return None
    idx = cmd.index("-f")
    if idx + 1 >= len(cmd) or cmd[idx + 1] == "-":
        return None
    path = cmd[idx + 1]
    return path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)


def exceeds_limits(app_name: str, manifest_yaml: str) -> Optional[str]:
    """Hermes không được tự nới tài nguyên: trả về lý do nếu manifest khai báo vượt gói"""
    limits = get_resources(app_name)
    if not limits:
        return None
    max_cpu, max_mem = cpu_millis(limits["cpu"]), memory_mi(limits["memory"])
    for doc in yaml.safe_load_all(manifest_yaml):
        if not isinstance(doc, dict) or doc.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        pod = (((doc.get("spec") or {}).get("template") or {}).get("spec")) or {}
        for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            lim = ((c or {}).get("resources") or {}).get("limits") or {}
            cpu = _parse_cpu_millis(lim.get("cpu")) if lim.get("cpu") is not None else None
            mem = _parse_quantity_mi(lim.get("memory")) if lim.get("memory") is not None else None
            if (cpu is not None and cpu > max_cpu) or (mem is not None and mem > max_mem):
                return (f"Manifest khai báo tài nguyên vượt gói ({limits['cpu']} CPU, {limits['memory']} RAM). "
                        f"Muốn tăng tài nguyên, hãy nâng gói trên web.")
    return None


# ─────────────────────────── Áp lên app đang chạy ───────────────────────────

def apply_to_running(app_name: str, namespace: str, workloads: List[str]) -> List[str]:
    """kubectl set resources cho các workload đang tồn tại của app; trả về danh sách đã cập nhật"""
    limits = get_resources(app_name)
    if not limits:
        return []
    req = requests_for(limits)
    updated = []
    for kind in ("deployment", "statefulset"):
        for name in sorted(workloads):
            res = subprocess.run(
                ["kubectl", "set", "resources", f"{kind}/{name}", "-n", namespace,
                 f"--limits=cpu={limits['cpu']},memory={limits['memory']}",
                 f"--requests=cpu={req['cpu']},memory={req['memory']}"],
                capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                updated.append(f"{kind}/{name}")
    return updated


def limit_range_manifest(namespace: str) -> Dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": LIMIT_RANGE_NAME, "namespace": namespace},
        "spec": {"limits": [{
            "type": "Container",
            "default": {"cpu": DEFAULT_LIMIT_CPU, "memory": DEFAULT_LIMIT_MEMORY},
            "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
        }]},
    }


def ensure_limit_range(namespace: str) -> bool:
    try:
        res = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(limit_range_manifest(namespace)),
                             capture_output=True, text=True, timeout=60)
        return res.returncode == 0
    except Exception:
        return False
