import json
import subprocess

from fastapi.testclient import TestClient

import main
import usage
from conftest import API_KEY

H = {"X-API-Key": API_KEY}

NODES = {"items": [{"metadata": {"name": "n1"}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}}]}
SUMMARY = {
    "node": {"cpu": {"usageNanoCores": 1_500_000_000}, "memory": {"workingSetBytes": 4 * 1024 ** 3},
             "fs": {"usedBytes": 20 * 1024 ** 3, "capacityBytes": 60 * 1024 ** 3}},
    "pods": [
        {"podRef": {"name": "shop-7d9fbc6c5d-x2k4p", "namespace": "default"},
         "cpu": {"usageNanoCores": 50_000_000}, "memory": {"workingSetBytes": 100 * 1024 ** 2},
         "ephemeral-storage": {"usedBytes": 5 * 1024 ** 2}},
        {"podRef": {"name": "shop-db-0", "namespace": "default"},
         "cpu": {"usageNanoCores": 8_000_000}, "memory": {"workingSetBytes": 300 * 1024 ** 2},
         "volume": [{"name": "db-storage", "usedBytes": 15 * 1024 ** 3}]},
        {"podRef": {"name": "shopping-6c8d9f7b5c-abcde", "namespace": "default"},
         "cpu": {"usageNanoCores": 999_000_000}, "memory": {"workingSetBytes": 999 * 1024 ** 2}},
    ],
}


def pod(name, limits=None, pvc=False):
    containers = [{"name": "c", "resources": {"limits": limits or {}},
                   "volumeMounts": [{"name": "db-storage", "mountPath": "/var/lib/mysql"}] if pvc else []}]
    return {"metadata": {"name": name}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 2}]},
            "spec": {"containers": containers,
                     "volumes": [{"name": "db-storage", "persistentVolumeClaim": {"claimName": "shop-db-pvc"}}] if pvc else []}}


PODS = {"items": [pod("shop-7d9fbc6c5d-x2k4p", {"cpu": "500m", "memory": "512Mi"}),
                  pod("shop-db-0", {"cpu": "1", "memory": "1Gi"}, pvc=True),
                  pod("shopping-6c8d9f7b5c-abcde", {"cpu": "2", "memory": "2Gi"})]}
PVCS = {"items": [{"metadata": {"name": "shop-db-pvc"}, "status": {"capacity": {"storage": "1Gi"}}}]}


def fake_kubectl(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "stats/summary" in joined:
            out = SUMMARY
        elif cmd[:3] == ["kubectl", "get", "nodes"]:
            out = NODES
        elif cmd[:3] == ["kubectl", "get", "pods"]:
            out = PODS
        elif cmd[:3] == ["kubectl", "get", "pvc"]:
            out = PVCS
        elif cmd[:2] == ["kubectl", "exec"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="220280\t/var/lib/mysql\n", stderr="")
        else:
            out = {}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
    monkeypatch.setattr(usage.subprocess, "run", run)
    monkeypatch.setattr(usage, "_stats_cache", (0.0, {}))
    usage._du_cache.clear()
    return calls


def test_usage_sums_only_the_apps_own_pods(monkeypatch):
    calls = fake_kubectl(monkeypatch)
    res = usage.apps_usage(["shop"], include_nodes=True)
    shop = res["apps"]["shop"]
    assert shop["cpu_m"] == 58 and shop["memory_mi"] == 400  # không cộng Pod của app "shopping"
    assert shop["cpu_limit_m"] == 1500 and shop["memory_limit_mi"] == 1536
    assert not shop["cpu_unlimited"] and not shop["memory_unlimited"]
    # Database đo bằng du trong Pod (215 MiB), không dùng usedBytes 15 GB của kubelet
    assert shop["db_used_mi"] == 215 and shop["db_capacity_mi"] == 1024
    assert len(shop["pods"]) == 2 and shop["pods"][0]["restarts"] == 2
    assert res["nodes"][0]["cpu_capacity_m"] == 4000 and res["nodes"][0]["disk_used_mi"] == 20480
    exec_calls = [c for c in calls if c[:2] == ["kubectl", "exec"]]
    assert exec_calls and exec_calls[0][-3:] == ["du", "-sk", "/var/lib/mysql"]


def test_du_is_cached(monkeypatch):
    calls = fake_kubectl(monkeypatch)
    usage.apps_usage(["shop"])
    usage.apps_usage(["shop"])
    assert len([c for c in calls if c[:2] == ["kubectl", "exec"]]) == 1


def test_usage_api_validates_and_hides_nodes(monkeypatch):
    fake_kubectl(monkeypatch)
    client = TestClient(main.app)
    res = client.post("/api/usage", json={"apps": ["shop"]}, headers=H)
    assert res.status_code == 200 and "nodes" not in res.json()
    assert client.post("/api/usage", json={"apps": ["../etc"]}, headers=H).status_code == 400


def test_domain_check_validates_input(monkeypatch):
    monkeypatch.setattr("hermes_tools.probe_domain", lambda d, timeout=10: {"domain": d, "ok": True, "status_code": 200,
                                                                          "response_ms": 5, "cert_days_left": 80})
    client = TestClient(main.app)
    res = client.post("/api/domains/check", json={"domains": ["Shop.Vibe.quyenlt.com", "shop.vibe.quyenlt.com"]}, headers=H)
    assert res.status_code == 200 and [r["domain"] for r in res.json()["results"]] == ["shop.vibe.quyenlt.com"]
    assert client.post("/api/domains/check", json={"domains": ["http://evil/x"]}, headers=H).status_code == 400


def test_pod_without_limits_is_marked_unlimited(monkeypatch):
    fake_kubectl(monkeypatch)
    PODS["items"].append(pod("blog-5d4f8c7b9d-bcdfg"))
    try:
        blog = usage.apps_usage(["blog"])["apps"]["blog"]
    finally:
        PODS["items"].pop()
    assert blog["cpu_unlimited"] and blog["memory_unlimited"] and blog["cpu_limit_m"] == 0
