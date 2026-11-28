"""
Đo tài nguyên đang dùng của các ứng dụng (trang Usage trên web).

- CPU, RAM: lấy từ kubelet (/api/v1/nodes/<node>/proxy/stats/summary) của mọi node, cộng theo Pod của app
  (nhận diện Pod bằng app_scope.owns_object, so khớp chính xác tên workload của app).
- Giới hạn: tổng resources.limits của các container trong Pod đang chạy (do gói hosting áp vào).
- Database: dung lượng THẬT đo bằng `du -sk <mountPath>` bên trong Pod có gắn PVC. Không dùng usedBytes của kubelet
  vì với local-path (hostPath) kubelet báo dung lượng của cả ổ đĩa node (vd 15 GB trong khi dữ liệu chỉ 215 MB).
  Kết quả du được lưu tạm DU_CACHE_SECONDS để không exec vào Pod quá thường xuyên.
- Ổ đĩa tạm (ephemeral-storage): log và file ghi trong container, lấy từ kubelet.
Admin xem thêm tài nguyên của từng node (node_summary).
"""

import json
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import app_scope
from builder_core import K8S_NAMESPACE

STATS_CACHE_SECONDS = 15
DU_CACHE_SECONDS = 300

_lock = threading.Lock()
_stats_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
_du_cache: Dict[str, Tuple[float, Optional[int]]] = {}


def _kubectl_json(args: List[str], timeout: int = 30) -> Dict[str, Any]:
    res = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:200])
    return json.loads(res.stdout or "{}")


def _cpu_m(value: Any) -> int:
    s = str(value or "0")
    if s.endswith("m"):
        return int(float(s[:-1]))
    if s.endswith("n"):
        return int(float(s[:-1]) / 1e6)
    try:
        return int(float(s) * 1000)
    except ValueError:
        return 0


def _mem_mi(value: Any) -> int:
    s = str(value or "0")
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 ** 2,
             "K": 1e3 / 1024 ** 2, "M": 1e6 / 1024 ** 2, "G": 1e9 / 1024 ** 2, "T": 1e12 / 1024 ** 2}
    for suffix in ("Ki", "Mi", "Gi", "Ti", "K", "M", "G", "T"):
        if s.endswith(suffix):
            try:
                return int(float(s[: -len(suffix)]) * units[suffix])
            except ValueError:
                return 0
    try:
        return int(float(s) / 1024 ** 2)
    except ValueError:
        return 0


def node_stats() -> Dict[str, Any]:
    """Thống kê kubelet của mọi node (cache ngắn): {"nodes": [...], "pods": {tên pod: stats}}"""
    global _stats_cache
    with _lock:
        if time.time() - _stats_cache[0] < STATS_CACHE_SECONDS and _stats_cache[1]:
            return _stats_cache[1]
    nodes = _kubectl_json(["get", "nodes", "-o", "json"])
    result: Dict[str, Any] = {"nodes": [], "pods": {}}
    for node in nodes.get("items", []):
        name = node["metadata"]["name"]
        cap = node.get("status", {}).get("allocatable", {})
        try:
            summary = _kubectl_json(["get", "--raw", f"/api/v1/nodes/{name}/proxy/stats/summary"])
        except Exception:
            summary = {}
        n = summary.get("node", {})
        fs = n.get("fs", {})
        result["nodes"].append({
            "name": name,
            "cpu_m": int((n.get("cpu", {}).get("usageNanoCores") or 0) / 1e6),
            "cpu_capacity_m": _cpu_m(cap.get("cpu")),
            "memory_mi": int((n.get("memory", {}).get("workingSetBytes") or 0) / 1024 ** 2),
            "memory_capacity_mi": _mem_mi(cap.get("memory")),
            "disk_used_mi": int((fs.get("usedBytes") or 0) / 1024 ** 2),
            "disk_capacity_mi": int((fs.get("capacityBytes") or 0) / 1024 ** 2),
        })
        for pod in summary.get("pods", []):
            if pod.get("podRef", {}).get("namespace") == K8S_NAMESPACE:
                result["pods"][pod["podRef"]["name"]] = pod
    with _lock:
        _stats_cache = (time.time(), result)
    return result


def _du_mi(pod: str, container: str, path: str) -> Optional[int]:
    key = f"{pod}/{container}:{path}"
    cached = _du_cache.get(key)
    if cached and time.time() - cached[0] < DU_CACHE_SECONDS:
        return cached[1]
    value: Optional[int] = None
    try:
        res = subprocess.run(["kubectl", "exec", "-n", K8S_NAMESPACE, pod, "-c", container, "--",
                              "du", "-sk", path], capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout.split():
            value = int(res.stdout.split()[0]) // 1024
    except Exception:
        value = None
    _du_cache[key] = (time.time(), value)
    return value


def _empty_app() -> Dict[str, Any]:
    return {"cpu_m": 0, "memory_mi": 0, "cpu_limit_m": 0, "memory_limit_mi": 0, "ephemeral_mi": 0,
            "db_used_mi": None, "db_capacity_mi": 0, "pods": []}


def apps_usage(apps: List[str], include_nodes: bool = False) -> Dict[str, Any]:
    stats = node_stats()
    pods = _kubectl_json(["get", "pods", "-n", K8S_NAMESPACE, "-o", "json"]).get("items", [])
    pvcs = _kubectl_json(["get", "pvc", "-n", K8S_NAMESPACE, "-o", "json"]).get("items", [])
    bases = sorted({app_scope.base_app_name(a) for a in apps if a})
    out: Dict[str, Dict[str, Any]] = {a: _empty_app() for a in bases}

    for pod in pods:
        name = pod["metadata"]["name"]
        app = next((a for a in bases if app_scope.owns_object(a, name)), None)
        if not app or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        entry = out[app]
        spec = pod.get("spec", {})
        cpu_limit = sum(_cpu_m(((c.get("resources") or {}).get("limits") or {}).get("cpu")) for c in spec.get("containers", []))
        mem_limit = sum(_mem_mi(((c.get("resources") or {}).get("limits") or {}).get("memory")) for c in spec.get("containers", []))
        st = stats["pods"].get(name, {})
        cpu = int((st.get("cpu", {}).get("usageNanoCores") or 0) / 1e6)
        mem = int((st.get("memory", {}).get("workingSetBytes") or 0) / 1024 ** 2)
        eph = int((st.get("ephemeral-storage", {}).get("usedBytes") or 0) / 1024 ** 2)
        restarts = sum(cs.get("restartCount", 0) for cs in pod.get("status", {}).get("containerStatuses", []) or [])
        entry["cpu_m"] += cpu
        entry["memory_mi"] += mem
        entry["cpu_limit_m"] += cpu_limit
        entry["memory_limit_mi"] += mem_limit
        entry["ephemeral_mi"] += eph
        entry["pods"].append({"name": name, "phase": pod.get("status", {}).get("phase"), "cpu_m": cpu,
                              "memory_mi": mem, "cpu_limit_m": cpu_limit, "memory_limit_mi": mem_limit,
                              "restarts": restarts})
        # Dung lượng database thật: du trong Pod tại thư mục gắn PVC
        claim_volumes = {v["name"] for v in spec.get("volumes", []) or [] if v.get("persistentVolumeClaim")}
        for c in spec.get("containers", []):
            for mount in c.get("volumeMounts", []) or []:
                if mount.get("name") in claim_volumes and pod.get("status", {}).get("phase") == "Running":
                    used = _du_mi(name, c["name"], mount["mountPath"])
                    if used is not None:
                        entry["db_used_mi"] = (entry["db_used_mi"] or 0) + used

    for pvc in pvcs:
        name = pvc["metadata"]["name"]
        app = next((a for a in bases if app_scope.owns_resource(a, name)), None)
        if app:
            size = (pvc.get("status", {}).get("capacity") or {}).get("storage") or \
                (pvc.get("spec", {}).get("resources", {}).get("requests") or {}).get("storage")
            out[app]["db_capacity_mi"] += _mem_mi(size)

    result: Dict[str, Any] = {"status": "success", "apps": out, "measured_at": int(time.time())}
    if include_nodes:
        result["nodes"] = stats["nodes"]
    return result
