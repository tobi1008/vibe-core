import subprocess

import pytest
import yaml
from fastapi.testclient import TestClient

import app_scope
import builder_core
import hermes_agent_router
import main
import package_limits as pl
from conftest import API_KEY

H = {"X-API-Key": API_KEY}
FREE = {"cpu": "250m", "memory": "256Mi", "db_storage": "2Gi", "allow_database": False}

AI_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: shop}
spec:
  template:
    spec:
      initContainers:
      - {name: migrate, image: x}
      containers:
      - name: web
        image: x
        resources: {limits: {cpu: "4", memory: 8Gi}}
      - {name: sidecar, image: y}
---
apiVersion: v1
kind: Service
metadata: {name: shop-svc}
spec: {ports: [{port: 80}]}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: shop-db-pvc}
spec: {resources: {requests: {storage: 50Gi}}}
"""


@pytest.fixture
def no_kubectl(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
    monkeypatch.setattr(pl.subprocess, "run", run)
    return calls


def test_normalize_rejects_bad_values():
    for bad in ({"cpu": "2", "memory": "1Gi"}, {"cpu": "500m", "memory": "1G"},
                {"cpu": "500m", "memory": "512Mi", "db_storage": "100Mi"}, {"cpu": "1m", "memory": "512Mi"}):
        with pytest.raises(pl.LimitsError):
            pl.normalize(bad)
    assert pl.normalize({"cpu": "500m", "memory": "1Gi"})["allow_database"] is True


def test_enforce_overrides_ai_limits_and_pvc(paths, no_kubectl):
    pl.save_resources("shop", FREE)
    docs = list(yaml.safe_load_all(pl.enforce_text("shop", AI_MANIFEST)))
    pod = docs[0]["spec"]["template"]["spec"]
    for c in pod["containers"] + pod["initContainers"]:
        assert c["resources"] == {"limits": {"cpu": "250m", "memory": "256Mi"},
                                  "requests": {"cpu": "62m", "memory": "64Mi"}}
    assert docs[1]["kind"] == "Service"
    assert docs[2]["spec"]["resources"]["requests"]["storage"] == "2Gi"


def test_existing_pvc_keeps_its_size(paths, monkeypatch):
    pl.save_resources("shop", FREE)
    monkeypatch.setattr(pl.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="1Gi", stderr=""))
    docs = list(yaml.safe_load_all(pl.enforce_text("shop", AI_MANIFEST)))
    assert docs[2]["spec"]["resources"]["requests"]["storage"] == "1Gi"


def test_app_without_package_is_untouched(paths):
    assert pl.enforce_text("shop", AI_MANIFEST) == AI_MANIFEST


def test_component_apps_share_base_limits(paths, no_kubectl):
    pl.save_resources("shop", FREE)
    assert pl.get_resources("shop-be")["cpu"] == "250m"


def test_run_cmd_enforces_before_kubectl_apply(paths, no_kubectl, monkeypatch, tmp_path):
    pl.save_resources("shop", FREE)
    manifest = tmp_path / "k8s" / "deployment.yaml"
    manifest.parent.mkdir()
    manifest.write_text(AI_MANIFEST)
    ran = []
    monkeypatch.setattr(builder_core.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    builder_core.run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], "shop", cwd=str(tmp_path))
    applied = list(yaml.safe_load_all(manifest.read_text()))
    assert applied[0]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "250m"
    assert ran[-1][:2] == ["kubectl", "apply"]


def test_apply_target_parsing(tmp_path):
    assert pl.apply_target(["kubectl", "apply", "-f", "a.yaml"], str(tmp_path)) == str(tmp_path / "a.yaml")
    assert pl.apply_target(["kubectl", "apply", "-f", "/x/b.yaml"], None) == "/x/b.yaml"
    assert pl.apply_target(["kubectl", "apply", "-f", "-"], None) is None
    assert pl.apply_target(["kubectl", "delete", "-f", "a.yaml"], None) is None


def test_hermes_cannot_raise_limits_beyond_package(paths, no_kubectl, monkeypatch):
    pl.save_resources("shop", FREE)
    monkeypatch.setattr("hermes_tools.app_domains", lambda app: ["shop.vibe.quyenlt.com"])
    manifest = AI_MANIFEST.split("---")[0]
    assert "vượt gói" in pl.exceeds_limits("shop", manifest)
    res = hermes_agent_router.execute_remediation_action(
        "shop", hermes_agent_router.RemediationAction(action_type="apply_manifest", content=manifest, description="t"))
    assert res["status"] == "error" and "vượt gói" in res["message"]
    ok = manifest.replace('cpu: "4", memory: 8Gi', "cpu: 200m, memory: 128Mi")
    assert pl.exceeds_limits("shop", ok) is None


def test_resources_api_saves_and_applies(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(pl.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    client = TestClient(main.app)
    res = client.post("/api/apps/shop/resources", json=FREE, headers=H)
    assert res.status_code == 200 and res.json()["resources"]["cpu"] == "250m"
    names = {c[3].split("/")[1] for c in calls if c[:3] == ["kubectl", "set", "resources"]}
    assert names == app_scope.workload_names("shop")
    assert client.post("/api/apps/shop/resources", json={"cpu": "9", "memory": "1Gi"}, headers=H).status_code == 400
    # Không tạo dòng "app ma" trong apps_config.json
    assert not main.app_config_exists("shop")


def test_database_flows_blocked_when_package_has_no_db(paths, no_kubectl, monkeypatch):
    pl.save_resources("shop", FREE)
    client = TestClient(main.app)
    res = client.post("/api/deploy-open-source", headers=H, json={
        "repo_url": "https://github.com/a/b", "app_name": "shop", "app_domain": "shop.vibe.quyenlt.com"})
    assert res.status_code == 403 and "database" in res.json()["detail"]
    res = client.post("/api/deploy-monorepo", headers=H, json={
        "repo_url": "https://github.com/a/b", "app_name": "shop", "be_domain": "api.shop.vn", "db_type": "none"})
    assert res.status_code != 403


def test_purge_forgets_resources(paths, no_kubectl):
    pl.save_resources("shop", FREE)
    main.purge_app_workspace_and_configs("shop")
    assert pl.get_resources("shop") is None


def test_limit_range_manifest():
    m = pl.limit_range_manifest("default")
    assert m["kind"] == "LimitRange" and m["spec"]["limits"][0]["default"]["memory"] == pl.DEFAULT_LIMIT_MEMORY
