import json
import subprocess

import app_scope
import node_image_cleanup as nic


def test_manifest_targets_only_app_repos_on_given_node():
    m = nic.job_manifest("shop", "node-1", ["shop", "shop-be", "shop-fe"], "tobi1008", "default")
    spec = m["spec"]["template"]["spec"]
    assert spec["nodeName"] == "node-1" and spec["automountServiceAccountToken"] is False
    cmd = spec["containers"][0]["command"]
    assert cmd[:2] == ["sh", "-c"] and cmd[4:] == ["docker.io/tobi1008/", "shop", "shop-be", "shop-fe"]
    # So khớp chính xác tên repository; image có tag của repository khác thì không xóa
    assert "index(list" in cmd[2] and "other[$3]" in cmd[2]
    # Không mang nhãn app=..., để kubectl delete -l app=shop không xóa nhầm Job
    assert "app" not in m["metadata"]["labels"] and "app" not in m["spec"]["template"]["metadata"]["labels"]
    assert m["spec"]["ttlSecondsAfterFinished"] > 0 and m["spec"]["backoffLimit"] == 0


def test_job_name_is_valid_and_not_owned_by_any_app():
    name = nic.job_name("a" * 60 + "-x", "sv.vibehostquyenlt.com")
    assert len(name) <= 63 and name.startswith("img-clean-")
    assert not app_scope.owns_resource("shop", nic.job_name("shop", "n1"))


def test_cleanup_creates_job_per_node(monkeypatch):
    monkeypatch.setenv("NODE_IMAGE_CLEANUP", "1")
    calls, logs = [], []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        out = "n1 n2" if cmd[:3] == ["kubectl", "get", "nodes"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(nic.subprocess, "run", run)
    assert nic.cleanup_app_images("shop", ["shop"], "tobi1008", "default", lambda a, m: logs.append(m)) == 2
    applied = [json.loads(inp) for cmd, inp in calls if cmd[:2] == ["kubectl", "apply"]]
    assert [j["spec"]["template"]["spec"]["nodeName"] for j in applied] == ["n1", "n2"]
    assert "2 node" in logs[-1]


def test_cleanup_never_raises_when_kubectl_fails(monkeypatch):
    monkeypatch.setenv("NODE_IMAGE_CLEANUP", "1")
    monkeypatch.setattr(nic.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    logs = []
    assert nic.cleanup_app_images("shop", ["shop"], "tobi1008", "default", lambda a, m: logs.append(m)) == 0
    assert "bỏ qua" in logs[-1]


def test_disabled_does_nothing(monkeypatch):
    monkeypatch.setattr(nic.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert nic.cleanup_app_images("shop", ["shop"], "tobi1008", "default", lambda a, m: None) == 0
