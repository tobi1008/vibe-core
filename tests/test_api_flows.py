"""
Kiểm thử API cho các luồng triển khai fullstack, monorepo, đồng bộ repo và xem log runtime.
Hàm chạy nền và kubectl được giả lập.
"""

import json
import subprocess

import pytest

import main


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)


@pytest.fixture
def bg(monkeypatch):
    recs = {}
    for name in ("deploy_fullstack_app", "deploy_monorepo_app", "deploy_archive_with_db"):
        r = Recorder()
        monkeypatch.setattr(main, name, r)
        recs[name] = r
    monkeypatch.setattr(main, "resolve_github_owner_from_token", lambda token: "tobi" if token else None)
    return recs


def read_config(paths):
    return json.loads(paths.config.read_text())


# ---------- Fullstack 2 repo ----------

FULLSTACK = {
    "app_name": "shop", "be_repo_url": "https://github.com/u/shop-be", "be_domain": "api.shop.com",
    "fe_repo_url": "https://github.com/u/shop-fe", "fe_domain": "shop.com", "db_type": "mysql",
}


def test_fullstack_with_backup_file(client, paths, bg):
    res = client.post("/api/deploy-fullstack", data={**FULLSTACK, "github_token": "ghp_abc", "ai_provider": "claude"},
                      files={"backup_file": ("../dump.sql", b"CREATE TABLE t (id int);", "application/sql")})
    assert res.status_code == 200 and res.json()["status"] == "success"
    args = bg["deploy_fullstack_app"].calls[0]
    assert args[0] == "shop"
    assert args[1] == "https://ghp_abc@github.com/u/shop-be"
    assert args[3] == "https://ghp_abc@github.com/u/shop-fe"
    assert args[6].startswith(str(paths.backups)) and ".." not in args[6]
    assert args[7] == "claude"
    cfg = read_config(paths)["shop"]
    assert cfg["be_repo_url"] == FULLSTACK["be_repo_url"] and cfg["primary_github_owner"] == "tobi"
    assert "ghp_abc" not in json.dumps(cfg)
    assert "Fullstack" in (paths.logs / "shop.log").read_text()


def test_fullstack_without_backup(client, paths, bg):
    res = client.post("/api/deploy-fullstack", data=FULLSTACK)
    assert res.status_code == 200
    assert bg["deploy_fullstack_app"].calls[0][6] == ""
    assert read_config(paths)["shop"]["has_token"] is False


@pytest.mark.parametrize("field,value", [("app_name", "Shop_1"), ("be_repo_url", "file:///etc"), ("fe_repo_url", "git@x:y")])
def test_fullstack_rejects_invalid_input(client, bg, field, value):
    res = client.post("/api/deploy-fullstack", data={**FULLSTACK, field: value})
    assert res.status_code == 400
    assert bg["deploy_fullstack_app"].calls == []


# ---------- Monorepo ----------

def test_monorepo_deploy(client, paths, bg):
    res = client.post("/api/deploy-monorepo", json={"repo_url": "https://github.com/u/mono", "app_name": "mono",
                                                     "be_domain": "api.mono.com", "db_type": "postgres"})
    assert res.status_code == 200
    args = bg["deploy_monorepo_app"].calls[0]
    assert args == ("https://github.com/u/mono", "mono", "api.mono.com", "", "postgres", "agy")
    assert "Monorepo" in (paths.logs / "mono.log").read_text()
    assert read_config(paths)["mono"]["repo_url"] == "https://github.com/u/mono"


def test_monorepo_rejects_bad_repo(client, bg):
    res = client.post("/api/deploy-monorepo", json={"repo_url": "http://x", "app_name": "mono", "be_domain": "a.com"})
    assert res.status_code == 400
    assert bg["deploy_monorepo_app"].calls == []


# ---------- Đồng bộ repo ----------

def test_sync_repo_updates_config(client, paths, bg):
    res = client.post("/api/apps/shop/sync-repo", json={"github_token": "t", "be_repo_url": "https://github.com/u/be",
                                                        "fe_repo_url": "https://github.com/u/fe", "repo_url": "https://github.com/u/r"})
    assert res.status_code == 200
    cfg = res.json()["config"]
    assert cfg["primary_github_owner"] == "tobi"
    assert cfg["be_repo_url"].endswith("/be") and cfg["fe_repo_url"].endswith("/fe") and cfg["repo_url"].endswith("/r")


def test_sync_repo_rejects_bad_url(client, bg):
    assert client.post("/api/apps/shop/sync-repo", json={"repo_url": "file:///etc/passwd"}).status_code == 400


# ---------- Log runtime ----------

class Proc:
    def __init__(self, stdout="", stderr=""):
        self.stdout, self.stderr = stdout, stderr


def fake_kubectl(monkeypatch, pods, logs_now="", err="", logs_prev=""):
    calls = []
    monkeypatch.setattr(main.subprocess, "check_output", lambda cmd, **kw: calls.append(cmd) or pods)

    def run(cmd, **kw):
        calls.append(cmd)
        if "--previous" in cmd:
            return Proc(logs_prev)
        return Proc(logs_now, err)

    monkeypatch.setattr(main.subprocess, "run", run)
    return calls


def test_pod_logs_success(client, monkeypatch):
    calls = fake_kubectl(monkeypatch, "shop-be-abc shop-be-def", logs_now="server started")
    body = client.get("/api/pod-logs/shop?component=be&lines=50").json()
    assert body["status"] == "success" and body["pod_name"] == "shop-be-abc" and body["logs"] == "server started"
    assert "shop-be" in calls[0]
    assert "--tail=50" in calls[1]


def test_pod_logs_no_pod(client, monkeypatch):
    fake_kubectl(monkeypatch, "")
    assert client.get("/api/pod-logs/shop").json()["status"] == "warning"


def test_pod_logs_uses_previous_crash(client, monkeypatch):
    fake_kubectl(monkeypatch, "shop-1", logs_now="", err="container is waiting", logs_prev="Error: boom")
    body = client.get("/api/pod-logs/shop").json()
    assert "LẦN CRASH TRƯỚC" in body["logs"] and "Error: boom" in body["logs"]


def test_pod_logs_returns_stderr_when_no_previous(client, monkeypatch):
    fake_kubectl(monkeypatch, "shop-1", logs_now="", err="pod not ready")
    assert client.get("/api/pod-logs/shop").json()["logs"] == "pod not ready"


def test_pod_logs_empty_running_container(client, monkeypatch):
    fake_kubectl(monkeypatch, "shop-1")
    assert "chưa sinh thêm log" in client.get("/api/pod-logs/shop").json()["logs"]


def test_pod_logs_kubectl_error(client, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(main.subprocess, "check_output", boom)
    assert client.get("/api/pod-logs/shop").json()["status"] == "error"


def test_pod_logs_lines_limit(client):
    assert client.get("/api/pod-logs/shop?lines=5000").status_code == 422
