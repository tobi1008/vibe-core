"""
Phạm vi của một ứng dụng trên K3s: những tài nguyên nào thuộc về app này.

Mọi ứng dụng của mọi người dùng chạy chung một namespace, nên các thao tác Hermes thực hiện
(đổi biến môi trường, restart, chạy lệnh trong Pod, apply manifest) và dữ liệu gửi cho Hermes
(ingress, service, sự kiện K3s) đều phải được giới hạn trong đúng tài nguyên của app.

Tên tài nguyên được so khớp CHÍNH XÁC theo cách Vibe Core tự đặt tên khi triển khai
(builder_core, db_builder_core, monorepo_builder_core), không so khớp gần đúng hay theo tiền tố,
vì app "blog" và app "personal-blog" hoặc "blog-shop" dùng chung namespace.
"""

import re
from typing import Any, Dict, List, Optional, Set

import yaml

from user_directory import base_app_name

_COMPONENTS = ("", "-be", "-fe", "-monorepo")
_SUFFIXES = ("", "-svc", "-ingress", "-tls", "-db", "-db-pvc", "-db-svc")

# Ký tự K8s dùng cho hậu tố sinh ngẫu nhiên (không có nguyên âm, không có 0/1/3)
_K8S_RAND = "[bcdfghjklmnpqrstvwxz2456789]"
_POD_SUFFIX = re.compile(rf"^-(?:{_K8S_RAND}{{6,10}}(?:-{_K8S_RAND}{{5}})?|\d+)$")


class ScopeError(ValueError):
    """Thao tác vượt ra ngoài phạm vi của app"""


def workload_names(app_name: str) -> Set[str]:
    """Tên Deployment/StatefulSet có thể có của app"""
    base = base_app_name(app_name)
    names = {f"{base}{c}" for c in _COMPONENTS}
    names |= {f"{base}{c}-db" for c in _COMPONENTS}
    return names


def resource_names(app_name: str) -> Set[str]:
    """Mọi tên tài nguyên (Deployment, Service, Ingress, Secret TLS, PVC...) mà app được phép đụng tới"""
    base = base_app_name(app_name)
    return {f"{base}{c}{s}" for c in _COMPONENTS for s in _SUFFIXES}


def owns_resource(app_name: str, name: str) -> bool:
    return bool(name) and name in resource_names(app_name)


def owns_object(app_name: str, name: str) -> bool:
    """Tên một đối tượng bất kỳ (kể cả ReplicaSet/Pod sinh tự động như blog-be-7d9fbc6c5d-x2k4p)"""
    if owns_resource(app_name, name):
        return True
    for workload in workload_names(app_name):
        if name.startswith(workload) and _POD_SUFFIX.match(name[len(workload):]):
            return True
    return False


def require_resource(app_name: str, name: str) -> str:
    if not owns_resource(app_name, name):
        raise ScopeError(f"'{name}' không thuộc ứng dụng {base_app_name(app_name)}, từ chối thao tác.")
    return name


# ─────────────────────────── Lọc dữ liệu gửi cho Hermes ───────────────────────────

def filter_table(app_name: str, table: str, name_column: int = 0) -> str:
    """Lọc bảng `kubectl get ...` (ingress, svc): giữ dòng tiêu đề và các dòng của app.
    name_column là vị trí cột tên (0 khi lệnh có -n)"""
    lines = (table or "").splitlines()
    if not lines:
        return ""
    kept = [lines[0]]
    for line in lines[1:]:
        cols = line.split()
        if len(cols) > name_column and owns_resource(app_name, cols[name_column].split("/")[-1]):
            kept.append(line)
    return "\n".join(kept) if len(kept) > 1 else ""


def filter_events(app_name: str, table: str) -> str:
    """Lọc bảng `kubectl get events`: chỉ giữ sự kiện mà đối tượng (cột dạng kind/tên) thuộc app"""
    kept = []
    for line in (table or "").splitlines():
        for token in line.split():
            if "/" in token and not token.startswith("http"):
                if owns_object(app_name, token.split("/", 1)[1]):
                    kept.append(line)
                break
    return "\n".join(kept)


# ─────────────────────────── Kiểm tra manifest ───────────────────────────

ALLOWED_KINDS = {"Deployment", "StatefulSet", "Service", "Ingress", "ConfigMap", "Secret",
                 "PersistentVolumeClaim", "Middleware"}
_ALLOWED_VOLUME_TYPES = {"name", "persistentVolumeClaim", "configMap", "secret", "emptyDir"}


def _check_name(app: str, name: Optional[str], what: str) -> None:
    if not owns_resource(app, name or ""):
        raise ScopeError(f"{what} '{name}' không thuộc ứng dụng {base_app_name(app)}.")


def _check_labels(app: str, labels: Optional[Dict[str, Any]], what: str) -> None:
    # Nhãn "app" quyết định Service nào chọn Pod nào: không được gắn vào Pod/Service của app khác
    value = (labels or {}).get("app")
    if value is not None and not owns_resource(app, str(value)):
        raise ScopeError(f"{what} dùng nhãn app='{value}' của ứng dụng khác.")


def _check_pod_spec(app: str, spec: Dict[str, Any]) -> None:
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(key):
            raise ScopeError(f"Không cho phép {key}.")
    if spec.get("serviceAccountName") not in (None, "", "default") or spec.get("serviceAccount") not in (None, "", "default"):
        raise ScopeError("Không cho phép đổi serviceAccount.")
    if spec.get("nodeName"):
        raise ScopeError("Không cho phép chỉ định nodeName.")
    for vol in spec.get("volumes") or []:
        extra = set(vol) - _ALLOWED_VOLUME_TYPES
        if extra:
            raise ScopeError(f"Không cho phép volume kiểu {', '.join(sorted(extra))}.")
        if "persistentVolumeClaim" in vol:
            _check_name(app, vol["persistentVolumeClaim"].get("claimName"), "PVC")
        if "configMap" in vol:
            _check_name(app, vol["configMap"].get("name"), "ConfigMap")
        if "secret" in vol:
            _check_name(app, vol["secret"].get("secretName"), "Secret")
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        sc = container.get("securityContext") or {}
        if sc.get("privileged") or sc.get("allowPrivilegeEscalation"):
            raise ScopeError("Không cho phép container privileged.")
        if (sc.get("capabilities") or {}).get("add"):
            raise ScopeError("Không cho phép thêm capabilities.")
        for port in container.get("ports") or []:
            if port.get("hostPort"):
                raise ScopeError("Không cho phép hostPort.")
        for env in container.get("env") or []:
            ref = env.get("valueFrom") or {}
            for key in ("secretKeyRef", "configMapKeyRef"):
                if key in ref:
                    _check_name(app, ref[key].get("name"), "Biến môi trường tham chiếu")
        for env_from in container.get("envFrom") or []:
            for key in ("secretRef", "configMapRef"):
                if key in env_from:
                    _check_name(app, env_from[key].get("name"), "envFrom tham chiếu")


def validate_manifest(app_name: str, manifest_yaml: str, namespace: str, domains: List[str]) -> int:
    """Chỉ cho phép manifest sửa tài nguyên của chính app, trong namespace của Vibe Hosting.
    Trả về số đối tượng; vi phạm thì ném ScopeError"""
    try:
        docs = [d for d in yaml.safe_load_all(manifest_yaml) if d]
    except yaml.YAMLError as e:
        raise ScopeError(f"Manifest YAML không hợp lệ: {str(e)[:200]}")
    if not docs:
        raise ScopeError("Manifest rỗng.")
    allowed_hosts = {d.lower() for d in domains}
    for doc in docs:
        if not isinstance(doc, dict):
            raise ScopeError("Manifest không đúng cấu trúc.")
        kind = doc.get("kind")
        if kind == "List":
            raise ScopeError("Không cho phép manifest dạng List, hãy tách từng đối tượng.")
        if kind not in ALLOWED_KINDS:
            raise ScopeError(f"Không cho phép tạo/sửa đối tượng loại {kind}.")
        meta = doc.get("metadata") or {}
        if meta.get("namespace") not in (None, namespace):
            raise ScopeError(f"Chỉ được thao tác trong namespace {namespace}.")
        _check_name(app_name, meta.get("name"), kind)
        spec = doc.get("spec") or {}

        if kind in ("Deployment", "StatefulSet"):
            template = spec.get("template") or {}
            _check_labels(app_name, (template.get("metadata") or {}).get("labels"), "Pod")
            _check_labels(app_name, (spec.get("selector") or {}).get("matchLabels"), "Selector")
            _check_pod_spec(app_name, template.get("spec") or {})
            for claim in spec.get("volumeClaimTemplates") or []:
                _check_name(app_name, (claim.get("metadata") or {}).get("name"), "volumeClaimTemplate")
        elif kind == "Service":
            if spec.get("type") not in (None, "ClusterIP"):
                raise ScopeError("Service chỉ được dùng kiểu ClusterIP.")
            _check_labels(app_name, spec.get("selector"), "Service")
        elif kind == "Ingress":
            for rule in spec.get("rules") or []:
                host = (rule.get("host") or "").lower()
                if host not in allowed_hosts:
                    raise ScopeError(f"Tên miền '{host or '(trống)'}' không thuộc ứng dụng {base_app_name(app_name)}.")
                for path in (rule.get("http") or {}).get("paths") or []:
                    service = ((path.get("backend") or {}).get("service") or {}).get("name")
                    _check_name(app_name, service, "Service đích")
            if spec.get("defaultBackend"):
                raise ScopeError("Không cho phép defaultBackend.")
            for tls in spec.get("tls") or []:
                _check_name(app_name, tls.get("secretName"), "Secret TLS")
                for host in tls.get("hosts") or []:
                    if host.lower() not in allowed_hosts:
                        raise ScopeError(f"Tên miền '{host}' không thuộc ứng dụng {base_app_name(app_name)}.")
    return len(docs)
