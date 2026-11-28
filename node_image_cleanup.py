"""
Xóa image của một app đã bị xóa khỏi các node K3s.

Xóa Deployment/Pod không làm K3s xóa image đã kéo về node: kubelet chỉ dọn khi ổ đĩa vượt ngưỡng GC,
nên image của app đã xóa có thể nằm lại hàng chục GB. Kubernetes không có API xóa image trên node,
vì vậy Vibe Core tạo trên mỗi node một Job ngắn:
  • Dùng chính crictl có sẵn của K3s trên node (mount file, không tải thêm công cụ) và socket containerd.
  • Danh sách image cần xóa do Vibe Core truyền vào: chỉ các repository docker.io/<DOCKER_USER>/<app>,
    <app>-be, <app>-fe; so khớp chính xác tên repository nên không đụng image của app khác. Image có tag
    thuộc repository khác (cùng IMAGE ID) thì không xóa, vì crictl rmi sẽ xóa luôn tag đó.
  • Pod của app có thể chưa dừng hẳn (image còn đang dùng thì crictl từ chối xóa) nên thử lại tối đa
    khoảng 3 phút, sau đó Job tự hủy (ttlSecondsAfterFinished).
Job không mang nhãn app=<tên app>, để lệnh xóa theo nhãn của delete_app không xóa nhầm nó.
Đặt NODE_IMAGE_CLEANUP=0 để tắt.
"""

import hashlib
import json
import os
import subprocess
from typing import Any, Callable, Dict, List

JOB_IMAGE = os.getenv("NODE_IMAGE_CLEANUP_IMAGE", "busybox:1.36")
K3S_BIN = "/var/lib/rancher/k3s/data/current/bin/k3s"  # multicall: mount với tên crictl là chạy crictl
CRICTL_CONFIG = "/var/lib/rancher/k3s/agent/etc/crictl.yaml"
CONTAINERD_SOCK = "/run/k3s/containerd/containerd.sock"
ATTEMPTS = 18
RETRY_SECONDS = 10
JOB_LABEL = {"vibe-hosting/job": "node-image-cleanup"}

# $1 = tiền tố registry (docker.io/<user>/), các tham số sau là tên repository.
# crictl rmi xóa cả image (mọi tag cùng IMAGE ID), nên chỉ xóa image mà MỌI tag đều thuộc app;
# image dùng chung ID với repository khác (vd tag lại từ image khác) thì bỏ qua.
SCRIPT = f"""prefix="$1"; shift
repos=" $* "
i=0
while [ $i -lt {ATTEMPTS} ]; do
  ids=$(crictl images --no-trunc | awk -v p="$prefix" -v list="$repos" '
    NR > 1 {{
      mine = substr($1, 1, length(p)) == p && index(list, " " substr($1, length(p) + 1) " ")
      if (mine) own[$3] = 1; else other[$3] = 1
    }}
    END {{ for (id in own) if (!(id in other)) print id }}')
  ids=$(echo $ids)
  if [ -z "$ids" ]; then echo "Không còn image nào của app trên node."; exit 0; fi
  echo "Xóa image: $ids"
  crictl rmi $ids && exit 0
  i=$((i+1)); sleep {RETRY_SECONDS}
done
echo "Còn image đang được dùng, để kubelet GC dọn sau."
"""


def enabled() -> bool:
    return os.getenv("NODE_IMAGE_CLEANUP", "1") != "0"


def job_name(app_name: str, node: str) -> str:
    digest = hashlib.sha1(node.encode()).hexdigest()[:6]
    return f"img-clean-{app_name[:40].rstrip('-')}-{digest}"


def job_manifest(app_name: str, node: str, repos: List[str], registry_user: str, namespace: str) -> Dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name(app_name, node), "namespace": namespace, "labels": dict(JOB_LABEL)},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": ATTEMPTS * RETRY_SECONDS + 120,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": dict(JOB_LABEL)},
                "spec": {
                    "nodeName": node,
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [{
                        "name": "cleanup",
                        "image": JOB_IMAGE,
                        "command": ["sh", "-c", SCRIPT, "cleanup", f"docker.io/{registry_user}/", *repos],
                        "resources": {"limits": {"cpu": "200m", "memory": "64Mi"}},
                        "volumeMounts": [
                            {"name": "crictl", "mountPath": "/usr/local/bin/crictl", "readOnly": True},
                            {"name": "crictl-config", "mountPath": "/etc/crictl.yaml", "readOnly": True},
                            {"name": "containerd", "mountPath": CONTAINERD_SOCK},
                        ],
                    }],
                    "volumes": [
                        {"name": "crictl", "hostPath": {"path": K3S_BIN, "type": "File"}},
                        {"name": "crictl-config", "hostPath": {"path": CRICTL_CONFIG, "type": "File"}},
                        {"name": "containerd", "hostPath": {"path": CONTAINERD_SOCK, "type": "Socket"}},
                    ],
                },
            },
        },
    }


def list_nodes() -> List[str]:
    res = subprocess.run(["kubectl", "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"],
                         capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:200])
    return res.stdout.split()


def cleanup_app_images(app_name: str, repos: List[str], registry_user: str, namespace: str,
                       log: Callable[[str, str], None]) -> int:
    """Tạo Job xóa image trên mỗi node; trả về số Job đã tạo. Lỗi chỉ ghi log, không làm hỏng việc xóa app"""
    if not enabled() or not repos:
        return 0
    try:
        nodes = list_nodes()
    except Exception as e:
        log(app_name, f"⚠️ Không lấy được danh sách node K3s, bỏ qua xóa image trên node: {e}")
        return 0
    created = 0
    for node in nodes:
        manifest = job_manifest(app_name, node, repos, registry_user, namespace)
        name = manifest["metadata"]["name"]
        subprocess.run(["kubectl", "delete", "job", name, "-n", namespace, "--ignore-not-found=true"],
                       capture_output=True, text=True, timeout=60)
        res = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest),
                             capture_output=True, text=True, timeout=60)
        if res.returncode == 0:
            created += 1
        else:
            log(app_name, f"⚠️ Không tạo được Job xóa image trên node {node}: {(res.stderr or res.stdout).strip()[:200]}")
    if created:
        log(app_name, f"    Đã giao {created} node K3s xóa image của app (chạy nền, tối đa ~3 phút).")
    return created
