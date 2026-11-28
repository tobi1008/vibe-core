"""
Cô lập mạng giữa các ứng dụng trên K3s bằng NetworkPolicy (K3s thực thi qua kube-router).

Mọi app của mọi user chạy chung namespace: không có NetworkPolicy thì Pod của app A gọi thẳng được tới
database / service của app B (vd `demo-db-svc:3306`). Chính sách:

1. `vibe-default-deny-ingress`: chặn mọi kết nối VÀO Pod trong namespace, trừ khi một policy khác cho phép.
2. `vibe-allow-ingress-controller`: cho Traefik (ingress controller ở kube-system) và kubelet trên node
   (health probe, đi từ địa chỉ cổng cni0 của node) vào mọi Pod. Truy cập qua tên miền công khai vẫn hoạt động,
   kể cả chứng chỉ Let's Encrypt (Pod giải HTTP-01 của cert-manager nhận traffic qua Traefik).
3. `vibe-app-<app>`: cho các thành phần CỦA CÙNG app gọi nhau (frontend → backend → database).

Chỉ chặn chiều vào (Ingress): app vẫn gọi ra Internet, DNS và các API bên ngoài như bình thường; app A cũng không
gọi được app B qua địa chỉ nội bộ, chỉ qua tên miền công khai (đi qua Traefik) như người ngoài.

Policy của app được tạo TRƯỚC khi Pod của app chạy (ensure_app trong builder_core.run_cmd, ngay trước
`kubectl apply`), và lúc API khởi động cho mọi app đang có, trước rồi mới bật deny mặc định, để không app nào
mất kết nối tới database của chính nó. Xóa app thì xóa policy của app. Hermes không tạo/sửa được NetworkPolicy
(không nằm trong app_scope.ALLOWED_KINDS). NETWORK_POLICY=0 để tắt.
"""

import json
import os
import subprocess
import threading
import time
from typing import Any, Dict, List

from app_scope import workload_names
from user_directory import base_app_name

DENY_NAME = "vibe-default-deny-ingress"
INGRESS_NAME = "vibe-allow-ingress-controller"
APP_PREFIX = "vibe-app-"
MANAGED_LABELS = {"app.kubernetes.io/managed-by": "vibe-core", "vibe-hosting/policy": "network-isolation"}

_ensured: Dict[str, float] = {}
_ENSURE_TTL = 600
_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("NETWORK_POLICY", "1") != "0"


def policy_name(app_name: str) -> str:
    return f"{APP_PREFIX}{base_app_name(app_name)}"[:63].rstrip("-")


def _selector(app_name: str) -> Dict[str, Any]:
    return {"matchExpressions": [{"key": "app", "operator": "In", "values": sorted(workload_names(app_name))}]}


def app_policy(app_name: str, namespace: str) -> Dict[str, Any]:
    base = base_app_name(app_name)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": policy_name(base), "namespace": namespace,
                     "labels": {**MANAGED_LABELS, "vibe-hosting/app": base}},
        "spec": {
            "podSelector": _selector(base),
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"podSelector": _selector(base)}]}],
        },
    }


def node_gateways() -> List[str]:
    """Địa chỉ cổng cni0 của từng node (x.x.x.1 trong podCIDR): nguồn của health probe từ kubelet"""
    try:
        res = subprocess.run(["kubectl", "get", "nodes", "-o", "jsonpath={.items[*].spec.podCIDR}"],
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    gateways = []
    for cidr in res.stdout.split():
        net = cidr.split("/")[0].split(".")
        if len(net) == 4:
            gateways.append(".".join(net[:3] + [str(int(net[3]) + 1)]) + "/32")
    return gateways


def baseline_policies(namespace: str, gateways: List[str]) -> List[Dict[str, Any]]:
    allow_from: List[Dict[str, Any]] = [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
    }]
    allow_from += [{"ipBlock": {"cidr": g}} for g in gateways]
    return [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": INGRESS_NAME, "namespace": namespace, "labels": dict(MANAGED_LABELS)},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": allow_from}]},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": DENY_NAME, "namespace": namespace, "labels": dict(MANAGED_LABELS)},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
        },
    ]


def _apply(manifests: List[Dict[str, Any]]) -> subprocess.CompletedProcess:
    doc = {"apiVersion": "v1", "kind": "List", "items": manifests}
    return subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(doc),
                          capture_output=True, text=True, timeout=60)


def ensure_app(app_name: str, namespace: str, force: bool = False) -> bool:
    """Tạo/cập nhật policy cho app (idempotent; bỏ qua nếu vừa làm trong _ENSURE_TTL giây)"""
    if not enabled() or not app_name:
        return False
    base = base_app_name(app_name)
    with _lock:
        if not force and time.time() - _ensured.get(base, 0) < _ENSURE_TTL:
            return True
    res = _apply([app_policy(base, namespace)])
    if res.returncode == 0:
        with _lock:
            _ensured[base] = time.time()
        return True
    return False


def delete_app(app_name: str, namespace: str) -> None:
    base = base_app_name(app_name)
    with _lock:
        _ensured.pop(base, None)
    subprocess.run(["kubectl", "delete", "networkpolicy", policy_name(base), "-n", namespace,
                    "--ignore-not-found=true"], capture_output=True, text=True, timeout=60)


def ensure_all(apps: List[str], namespace: str) -> Dict[str, Any]:
    """Lúc khởi động: policy cho mọi app TRƯỚC, rồi mới cho Traefik/kubelet vào và bật chặn mặc định"""
    if not enabled():
        return {"enabled": False}
    bases = sorted({base_app_name(a) for a in apps if a})
    failed = [b for b in bases if not ensure_app(b, namespace, force=True)]
    if failed:
        # Không bật chặn mặc định khi chưa chắc mọi app có policy, tránh làm app mất kết nối tới database
        return {"enabled": True, "apps": bases, "failed": failed, "baseline": False}
    res = _apply(baseline_policies(namespace, node_gateways()))
    return {"enabled": True, "apps": bases, "failed": [], "baseline": res.returncode == 0,
            "error": None if res.returncode == 0 else (res.stderr or res.stdout).strip()[:200]}
