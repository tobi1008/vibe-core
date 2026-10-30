import hashlib
import hmac
import json
import subprocess

import pytest

import main
import security


class Recorder:
    """Thay thế các hàm chạy nền (deploy/redeploy) để không đụng tới git/docker/kubectl"""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def rec(monkeypatch):
    recorders = {}
    for name in ("deploy_app", "deploy_app_with_db", "deploy_fullstack_app", "deploy_monorepo_app",
                 "deploy_archive_app", "deploy_archive_with_db", "deploy_cms_app", "redeploy_app",
                 "purge_app_workspace_and_configs"):
        r = Recorder()
        monkeypatch.setattr(main, name, r)
        recorders[name] = r
    return recorders


# ---------- Xác thực ----------

def test_missing_api_key_is_rejected(paths):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        assert c.get("/api/ai-providers").status_code == 401
        assert c.get("/api/ai-providers", headers={"X-API-Key": "wrong"}).status_code == 401


def test_server_without_api_key_returns_503(paths, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(security, "VIBE_API_KEY", "")
    with TestClient(main.app) as c:
        assert c.get("/api/ai-providers", headers={"X-API-Key": ""}).status_code == 503


def test_valid_api_key(client):
    res = client.get("/api/ai-providers")
    assert res.status_code == 200
    body = res.json()
    assert [p["id"] for p in body["providers"]] == ["agy", "claude"]
    assert body["verifier"]["id"] == "hermes"


@pytest.mark.parametrize("raw,expected", [("hermes", "agy"), (None, "agy"), (" Claude-Code ", "claude"), ("AGY", "agy")])
def test_config_engine_is_agy_or_claude(client, rec, raw, expected):
    payload = {"repo_url": "https://github.com/u/r", "app_name": "shop", "app_domain": "a.com"}
    if raw is not None:
        payload["ai_provider"] = raw
    client.post("/api/deploy", json=payload)
    assert rec["deploy_app"].calls[0][0][3] == expected


def test_invalid_app_name_in_path_rejected(client):
    assert client.get("/api/logs/Bad_Name").status_code == 400
    assert client.get("/api/apps/a;id/config").status_code == 400


# ---------- Deploy ----------

def test_deploy_schedules_build_and_saves_config(client, rec, no_github, paths):
    res = client.post("/api/deploy", json={
        "repo_url": "https://github.com/u/shop.git",
        "app_name": "shop",
        "app_domain": "shop.example.com",
        "github_token": "ghp_abcdefghijklmnopqrstuvwxyz",
        "ai_provider": " Claude ",
    })
    assert res.status_code == 200, res.text
    args, _ = rec["deploy_app"].calls[0]
    assert args == ("https://ghp_abcdefghijklmnopqrstuvwxyz@github.com/u/shop.git", "shop", "shop.example.com", "claude")

    cfg = json.loads(paths.config.read_text())["shop"]
    assert cfg["repo_url"] == "https://github.com/u/shop.git"
    assert cfg["primary_github_owner"] == "tester"
    assert cfg["has_token"] is True
    assert "ghp_" not in paths.config.read_text()
    assert (paths.logs / "shop.log").exists()


@pytest.mark.parametrize("payload", [
    {"repo_url": "http://github.com/u/r", "app_name": "shop", "app_domain": "a.com"},
    {"repo_url": "https://github.com/u/r", "app_name": "Shop!", "app_domain": "a.com"},
])
def test_deploy_rejects_invalid_input(client, rec, payload):
    assert client.post("/api/deploy", json=payload).status_code == 400
    assert rec["deploy_app"].calls == []


@pytest.mark.xfail(strict=True, reason="BUG: app_domain chưa được kiểm tra, bị ghép thẳng vào YAML Ingress")
def test_deploy_rejects_malformed_domain(client, rec):
    res = client.post("/api/deploy", json={
        "repo_url": "https://github.com/u/r", "app_name": "shop",
        "app_domain": "a.com\n---\nkind: ClusterRoleBinding",
    })
    assert res.status_code == 400


def test_deploy_open_source_defaults_to_wordpress(client, rec, no_github, paths):
    res = client.post("/api/deploy-open-source", json={
        "repo_url": "", "app_name": "blog", "app_domain": "blog.example.com",
    })
    assert res.status_code == 200
    args, _ = rec["deploy_cms_app"].calls[0]
    assert args[0] == "https://github.com/WordPress/WordPress.git"
    assert args[5] == "wordpress"


def test_deploy_archive_sanitizes_upload_name(client, rec, paths):
    res = client.post(
        "/api/deploy-archive",
        data={"app_name": "zipapp", "app_domain": "z.example.com"},
        files={"archive_file": ("../../evil name.zip", b"PK\x03\x04", "application/zip")},
    )
    assert res.status_code == 200, res.text
    saved = paths.uploads / "zipapp_evil_name.zip"
    assert saved.read_bytes() == b"PK\x03\x04"
    args, _ = rec["deploy_archive_app"].calls[0]
    assert args[0] == str(saved)


def test_deploy_with_db_saves_backup(client, rec, no_github, paths):
    res = client.post(
        "/api/deploy-with-db",
        data={"repo_url": "https://github.com/u/r", "app_name": "dbapp",
              "app_domain": "d.example.com", "db_type": "mysql"},
        files={"backup_file": ("dump.sql", b"CREATE TABLE t(id int);", "application/sql")},
    )
    assert res.status_code == 200, res.text
    assert (paths.backups / "dbapp_dump.sql").exists()
    args, _ = rec["deploy_app_with_db"].calls[0]
    assert args[3] == "mysql" and args[4].endswith("dbapp_dump.sql")


# ---------- Logs / config ----------

def test_logs_waiting_when_missing(client):
    assert client.get("/api/logs/nothing").json()["status"] == "waiting"


def test_logs_are_redacted(client, paths):
    (paths.logs / "shop.log").write_text("git clone https://ghp_abcdefghijklmnopqrstuvwxyz@github.com/u/r\n")
    logs = client.get("/api/logs/shop").json()["logs"]
    assert "ghp_" not in logs and "***@github.com" in logs


def test_toggle_auto_cicd(client):
    assert client.post("/api/apps/shop/config", json={"auto_cicd": False}).status_code == 200
    assert client.get("/api/apps/shop/config").json()["data"]["auto_cicd"] is False


def test_delete_schedules_purge(client, rec):
    assert client.delete("/api/apps/shop").status_code == 200
    assert rec["purge_app_workspace_and_configs"].calls == [(("shop",), {})]


def test_manual_redeploy_defaults(client, rec):
    assert client.post("/api/apps/shop/redeploy").status_code == 200
    _, kwargs = rec["redeploy_app"].calls[0]
    assert kwargs == {"app_name": "shop", "branch": "main", "triggered_repo_url": None,
                      "pull_git": True, "target_component": None}


# ---------- Webhook ----------

def _push(client, body: dict, secret: str = ""):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "X-API-Key": ""}
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/webhooks/github/shop", content=raw, headers=headers)


PUSH = {"ref": "refs/heads/main", "repository": {"clone_url": "https://github.com/u/shop.git"}}


def test_webhook_requires_valid_signature(client, rec, monkeypatch):
    monkeypatch.setattr(security, "GITHUB_WEBHOOK_SECRET", "hook")
    assert _push(client, PUSH).status_code == 401
    assert _push(client, PUSH, secret="wrong").status_code == 401
    assert rec["redeploy_app"].calls == []

    res = _push(client, PUSH, secret="hook")
    assert res.status_code == 200 and res.json()["status"] == "success"
    args, _ = rec["redeploy_app"].calls[0]
    assert args == ("shop", "main", "https://github.com/u/shop.git")


def test_webhook_ignored_when_auto_cicd_off(client, rec):
    client.post("/api/apps/shop/config", json={"auto_cicd": False})
    assert _push(client, PUSH).json()["status"] == "ignored"
    assert rec["redeploy_app"].calls == []


def test_webhook_rejects_bad_json(client, rec):
    res = client.post("/api/webhooks/github/shop", content=b"not json", headers={"X-API-Key": ""})
    assert res.status_code == 400


@pytest.mark.xfail(strict=True, reason="BUG: nhánh có dấu '/' (feature/login) bị cắt còn 'login'")
def test_webhook_branch_with_slash(client, rec):
    _push(client, {**PUSH, "ref": "refs/heads/feature/login"})
    args, _ = rec["redeploy_app"].calls[0]
    assert args[1] == "feature/login"


@pytest.mark.xfail(strict=True, reason="BUG: push tag (refs/tags/v1.0) vẫn kích hoạt redeploy với branch 'v1.0'")
def test_webhook_ignores_tag_push(client, rec):
    _push(client, {**PUSH, "ref": "refs/tags/v1.0"})
    assert rec["redeploy_app"].calls == []


# ---------- Rollback / history (giả lập kubectl) ----------

def test_rollback_builds_expected_command(client, monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="rolled back", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    res = client.post("/api/apps/shop/rollback", json={"component": "be", "revision": 3})
    assert res.json()["status"] == "success"
    assert seen[0] == "kubectl rollout undo deployment/shop-be --to-revision=3 -n default"


def rs(revision, ready, desired=1, hash_="h", created="2026-10-30T10:00:00Z", image="tobi1008/shop:latest"):
    return {
        "metadata": {"annotations": {"deployment.kubernetes.io/revision": str(revision)},
                     "labels": {"pod-template-hash": hash_}, "creationTimestamp": created},
        "spec": {"replicas": desired, "template": {"spec": {"containers": [{"image": image}]}}},
        "status": {"readyReplicas": ready},
    }


def deployment(revision, ready=1):
    return {"metadata": {"annotations": {"deployment.kubernetes.io/revision": str(revision)}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "shop"}}},
            "status": {"readyReplicas": ready}}


def test_history_uses_real_replicasets(client, kube):
    kube.workloads = {"deployment/shop": deployment(2)}
    kube.replicasets = [rs(1, 0, desired=0, hash_="a", created="2026-10-29T08:00:00Z"),
                        rs(2, 1, hash_="b", created="2026-10-30T09:00:00Z", image="tobi1008/shop:1761800000")]
    history = client.get("/api/apps/shop/history").json()["history"]
    assert [(h["revision"], h["state"], h["is_current"]) for h in history] == [(2, "running", True), (1, "previous", False)]
    assert history[0]["created_at"] == "2026-10-30T09:00:00Z"
    assert history[0]["image"] == "tobi1008/shop:1761800000"


def test_history_current_revision_crashing_is_failed(client, kube):
    kube.workloads = {"deployment/shop": deployment(2, ready=0)}
    kube.replicasets = [rs(1, 0, desired=0, hash_="a"), rs(2, 0, hash_="b")]
    crashing = {"metadata": {"name": "shop-b-1", "labels": {"pod-template-hash": "b"}},
                "status": {"phase": "Running", "containerStatuses": [
                    {"name": "shop", "ready": False, "restartCount": 3, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
    kube.pods = {"app=shop": [crashing]}
    history = client.get("/api/apps/shop/history").json()["history"]
    assert history[0]["state"] == "failed"


def test_history_marks_revision_hermes_found_failed(client, kube, paths):
    # Revision 2 từng deploy lỗi (Hermes ghi nhận), sau đó đã rollback về bản 3 (= nội dung bản 1)
    (paths.logs / "shop.revisions.json").write_text(json.dumps(
        {"deployment/shop": {"2": {"ready": False, "status": "code_error"}}}))
    kube.workloads = {"deployment/shop": deployment(3)}
    kube.replicasets = [rs(2, 0, desired=0, hash_="b"), rs(3, 1, hash_="a")]
    history = client.get("/api/apps/shop/history").json()["history"]
    assert [(h["revision"], h["state"]) for h in history] == [(3, "running"), (2, "failed")]


def test_history_starting_pod_is_not_running(client, kube):
    kube.workloads = {"deployment/shop": deployment(1, ready=0)}
    kube.replicasets = [rs(1, 0, hash_="a")]
    assert client.get("/api/apps/shop/history").json()["history"][0]["state"] == "starting"


def test_history_missing_deployment_404(client, kube):
    kube.workloads = {"deployment/shop": None}
    assert client.get("/api/apps/shop/history").status_code == 404


# ---------- Hermes Verifier endpoints ----------

def test_health_report_none_then_available(client, paths):
    assert client.get("/api/apps/shop/health-report").json()["status"] == "none"
    (paths.logs / "shop.health.json").write_text(json.dumps({"status": "healthy", "ready": True}))
    assert client.get("/api/apps/shop/health-report").json()["report"]["status"] == "healthy"


def test_verify_endpoint_discovers_workloads(client, kube, paths):
    kube.workloads = {"deployment.apps/shop": {}, "statefulset.apps/shop-db": {}, "deployment.apps/other": {}}
    res = client.post("/api/apps/shop/verify")
    assert res.json()["workloads"] == ["statefulset/shop-db", "deployment/shop"]


def test_verify_endpoint_404_without_workloads(client):
    assert client.post("/api/apps/shop/verify").status_code == 404
