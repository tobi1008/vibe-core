import json
import subprocess

import pytest

import builder_core
import dockerhub_cleanup

OLD = "2026-01-01T00:00:00Z"


def ts(day):
    return f"2026-02-{day:02d}T00:00:00Z"


class FakeHub:
    """Docker Hub giả: repos = {name: [(tag, last_updated), ...]}"""

    def __init__(self):
        self.repos = {}
        self.repo_updated = {}
        self.deleted = []

    def get(self, url, headers=None, timeout=None):
        assert headers == {"Authorization": "Bearer tok"}
        path = url.split("/v2/repositories/tobi1008/")[1].split("?")[0].strip("/")
        if not path:
            results = [{"name": n, "last_updated": self.repo_updated.get(n, OLD)} for n in self.repos]
        else:
            repo = path.split("/")[0]
            results = [{"name": t, "last_updated": u} for t, u in self.repos[repo]]
        return Resp(200, {"results": results, "next": None})

    def delete(self, url, headers=None, timeout=None):
        path = url.split("/v2/repositories/tobi1008/")[1].strip("/")
        self.deleted.append(path)
        return Resp(204)


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data, self.text = status, data or {}, ""

    def json(self):
        return self._data


@pytest.fixture
def hub(monkeypatch, paths):
    h = FakeHub()
    monkeypatch.setattr(dockerhub_cleanup.requests, "get", h.get)
    monkeypatch.setattr(dockerhub_cleanup.requests, "delete", h.delete)
    monkeypatch.setattr(dockerhub_cleanup, "dockerhub_token", lambda: "tok")
    monkeypatch.setenv("DOCKERHUB_PROTECTED_REPOS", "")
    return h


@pytest.fixture
def k3s(monkeypatch):
    """Ảnh K3s giả: danh sách image đang được workload tham chiếu"""
    state = {"images": [], "fail": False}

    def run(cmd, capture_output=True, text=True, timeout=None):
        if state["fail"]:
            return subprocess.CompletedProcess(cmd, 1, "", "connection refused")
        items = [{"kind": "ReplicaSet", "spec": {"template": {"spec": {"containers": [{"image": i}]}}}} for i in state["images"]]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"items": items}), "")

    monkeypatch.setattr(dockerhub_cleanup.subprocess, "run", run)
    return state


def test_orphan_repo_is_planned_but_used_repo_is_not(hub, k3s):
    hub.repos = {"profile": [("1795100798", OLD)], "shop": [("100", OLD)]}
    k3s["images"] = ["tobi1008/shop:100"]
    plan = dockerhub_cleanup.build_plan()
    assert [r["repo"] for r in plan["orphan_repos"]] == ["profile"]
    assert hub.deleted == []  # xem trước không xóa gì


def test_protected_repo_is_never_touched(hub, k3s, monkeypatch):
    monkeypatch.setenv("DOCKERHUB_PROTECTED_REPOS", "tobi1008/my-blog, vibe-core")
    hub.repos = {"my-blog": [("latest", OLD)], "vibe-core": [("1", OLD)], "profile": [("1", OLD)]}
    plan = dockerhub_cleanup.build_plan()
    assert [r["repo"] for r in plan["orphan_repos"]] == ["profile"]
    result = dockerhub_cleanup.execute(["my-blog", "profile"], {})
    assert result["refused"] == ["my-blog"]
    assert hub.deleted == ["profile"]


def test_repo_of_app_still_on_panel_is_not_orphan(hub, k3s, paths):
    (paths.ws / "shop-be").mkdir()
    hub.repos = {"shop-be": [("1", OLD)], "shop-fe": [("1", OLD)]}
    assert dockerhub_cleanup.build_plan()["orphan_repos"] == []


def test_recently_pushed_repo_is_skipped(hub, k3s, monkeypatch):
    from datetime import datetime, timezone
    hub.repos = {"profile": [("1", OLD)]}
    hub.repo_updated = {"profile": datetime.now(timezone.utc).isoformat()}
    plan = dockerhub_cleanup.build_plan()
    assert plan["orphan_repos"] == [] and plan["skipped"][0]["repo"] == "profile"


def test_old_tags_pruned_but_newest_and_rollback_tags_kept(hub, k3s, monkeypatch):
    monkeypatch.setattr(dockerhub_cleanup, "KEEP_TAGS", 2)
    hub.repos = {"shop": [(str(d), ts(d)) for d in range(1, 7)]}
    # tag 6 đang chạy, tag 2 là ReplicaSet cũ để rollback
    k3s["images"] = ["tobi1008/shop:6", "docker.io/tobi1008/shop:2"]
    plan = dockerhub_cleanup.build_plan()
    assert sorted(plan["old_tags"]["shop"]) == ["1", "3", "4"]


def test_execute_refuses_items_not_in_current_plan(hub, k3s):
    hub.repos = {"shop": [(str(d), ts(d)) for d in range(1, 6)]}
    k3s["images"] = ["tobi1008/shop:5"]
    result = dockerhub_cleanup.execute(["shop"], {"shop": ["1", "5"]})
    assert "shop" in result["refused"] and "shop:5" in result["refused"]
    assert hub.deleted == ["shop/tags/1"]


def test_nothing_deleted_when_k3s_unreadable(hub, k3s):
    k3s["fail"] = True
    hub.repos = {"profile": [("1", OLD)]}
    with pytest.raises(dockerhub_cleanup.CleanupError):
        dockerhub_cleanup.execute(["profile"], {})
    assert hub.deleted == []


def test_api_preview_and_execute(client, hub, k3s):
    hub.repos = {"profile": [("1", OLD)]}
    res = client.get("/api/dockerhub/cleanup")
    assert res.status_code == 200
    assert res.json()["plan"]["orphan_repos"][0]["repo"] == "profile"
    res = client.post("/api/dockerhub/cleanup", json={"repos": ["profile"]})
    assert res.json()["result"]["repos"] == {"profile": "deleted"}


def test_api_reports_login_failure(client, hub, k3s, monkeypatch):
    monkeypatch.setattr(dockerhub_cleanup, "dockerhub_token", lambda: None)
    res = client.get("/api/dockerhub/cleanup")
    assert res.status_code == 502 and "DOCKER_PASSWORD" in res.json()["detail"]
