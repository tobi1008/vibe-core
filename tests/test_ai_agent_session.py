import os
import subprocess

import pytest
from fastapi.testclient import TestClient

import ai_agent_session as agent
import hermes_agent_router
import main
from conftest import API_KEY

H = {"X-API-Key": API_KEY}
GOOD_DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata: {name: shop}
spec:
  selector: {matchLabels: {app: shop}}
  template:
    metadata: {labels: {app: shop}}
    spec:
      containers:
      - {name: shop, image: x, env: [{name: PORT, value: "8080"}]}
"""
BAD_DEPLOY = GOOD_DEPLOY.replace("containers:", "volumes: [{name: h, hostPath: {path: /}}]\n      containers:")


@pytest.fixture
def sandbox(tmp_path, monkeypatch, paths):
    ws = tmp_path / "ws"
    (ws / "k8s").mkdir(parents=True)
    (ws / "k8s" / "deployment.yaml").write_text(GOOD_DEPLOY)
    (ws / "app.js").write_text("console.log('v1')\n")
    (ws / "old.txt").write_text("bye\n")
    (ws / "node_modules" / "x").mkdir(parents=True)
    (ws / "node_modules" / "x" / "i.js").write_text("big")
    monkeypatch.setattr(agent, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(agent, "SESSIONS_DIR", str(tmp_path / "sessions"))
    (tmp_path / "jobs").mkdir()
    monkeypatch.setattr(agent, "available", lambda: True)
    monkeypatch.setattr(agent.pwd, "getpwnam", lambda u: type("P", (), {"pw_dir": "/home/vibe-ai"})())
    calls = {"cmd": None, "copied": None}
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "chown":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        calls["cmd"] = cmd
        cwd = kwargs["cwd"]
        calls["copied"] = sorted(os.listdir(cwd))
        # Agent "sửa" file trong bản sao
        open(os.path.join(cwd, "app.js"), "w").write("console.log('v2')\n")
        open(os.path.join(cwd, "new.js"), "w").write("export default 1\n")
        os.remove(os.path.join(cwd, "old.txt"))
        open(os.path.join(cwd, "k8s", "deployment.yaml"), "w").write(calls.get("manifest", GOOD_DEPLOY.replace('"8080"', '"3000"')))
        return subprocess.CompletedProcess(cmd, 0, "Đã sửa app.js và PORT.", "")
    monkeypatch.setattr(agent.subprocess, "run", run)
    yield {"ws": str(ws), "calls": calls, "real_run": real_run}


def start(sandbox, **kw):
    return agent.start_session("shop", "be", "sửa lỗi port", "agy", "u1", sandbox["ws"], {"pod_runtime_logs": "Error: EADDRINUSE"},
                               ["shop.vibe.quyenlt.com"], run_async=False, **kw)


def test_agent_runs_as_unprivileged_user_on_a_copy(sandbox):
    s = agent.load_session(start(sandbox)["id"])
    cmd = sandbox["calls"]["cmd"]
    assert cmd[:6] == ["sudo", "-n", "-u", "vibe-ai", "--", "env"] and "KUBECONFIG=/dev/null" in cmd
    assert not any(c.startswith("VIBE_API_KEY") for c in cmd)
    assert "node_modules" not in sandbox["calls"]["copied"]
    # Workspace thật không bị đụng tới
    assert open(os.path.join(sandbox["ws"], "app.js")).read() == "console.log('v1')\n"
    by_path = {c["path"]: c for c in s["changes"]}
    assert by_path["app.js"]["status"] == "modified" and "+console.log('v2')" in by_path["app.js"]["diff"]
    assert by_path["new.js"]["status"] == "added"
    assert by_path["old.txt"]["status"] == "deleted" and by_path["old.txt"]["blocked"]
    assert by_path["k8s/deployment.yaml"]["kind"] == "k8s" and not by_path["k8s/deployment.yaml"].get("blocked")
    assert s["status"] == "done" and "Đã sửa" in s["reply"]
    assert not os.path.exists(os.path.join(agent.JOBS_DIR, s["id"]))  # bản sao đã dọn


def test_manifest_with_hostpath_is_blocked(sandbox):
    sandbox["calls"]["manifest"] = BAD_DEPLOY
    s = agent.load_session(start(sandbox)["id"])
    k8s = next(c for c in s["changes"] if c["path"] == "k8s/deployment.yaml")
    assert "hostPath" in k8s["blocked"]


def test_apply_writes_applies_and_rebuilds_only_allowed(sandbox):
    sid = start(sandbox)["id"]
    executed = []
    res = agent.apply_session(sid, "shop", None, True, lambda app, a: executed.append(a["action_type"]) or {"status": "success"})
    # app.js, k8s/deployment.yaml, new.js được ghi; manifest được apply; có sửa code nên build lại một lần
    assert sorted(executed) == sorted(["edit_file", "edit_file", "edit_file", "apply_manifest", "rebuild"])
    assert executed[-1] == "rebuild"
    assert any(r["status"] == "skipped" for r in res["results"])  # old.txt (xóa) bị bỏ qua
    with pytest.raises(agent.AgentError, match="đã được áp dụng"):
        agent.apply_session(sid, "shop", None, True, lambda app, a: {"status": "success"})


def test_session_cannot_be_used_for_another_app(sandbox):
    sid = start(sandbox)["id"]
    with pytest.raises(agent.AgentError, match="không thuộc"):
        agent.apply_session(sid, "blog", None, True, lambda app, a: {"status": "success"})


def test_only_cli_engines_and_one_session_per_app(sandbox):
    with pytest.raises(agent.AgentError, match="Gemini 4 Hydrogen hoặc Claude"):
        agent.start_session("shop", "be", "x", "byo:abc", "u1", sandbox["ws"], {}, [], run_async=False)
    agent._running["shop"] = "busy"
    try:
        with pytest.raises(agent.AgentError, match="Đang có một phiên"):
            start(sandbox)
    finally:
        agent._running.pop("shop", None)


def test_agent_api_flow(sandbox, monkeypatch):
    monkeypatch.setattr(hermes_agent_router, "resolve_workspace", lambda app, comp="be": sandbox["ws"])
    monkeypatch.setattr(hermes_agent_router, "collect_debug_context", lambda app, comp="be": {})
    monkeypatch.setattr("hermes_tools.app_domains", lambda app: ["shop.vibe.quyenlt.com"])
    real_start = agent.start_session
    monkeypatch.setattr(agent, "start_session", lambda *a, **k: real_start(*a, **{**k, "run_async": False}))
    client = TestClient(main.app)
    res = client.post("/api/ai-debug/agent", headers=H, json={"app_name": "shop", "message": "sửa port", "ai_provider": "agy"})
    assert res.status_code == 200
    sid = res.json()["session"]["id"]
    got = client.get(f"/api/ai-debug/agent/{sid}?app_name=shop", headers=H).json()["session"]
    assert got["status"] == "done" and all("new_content" not in c for c in got["changes"])
    assert client.get(f"/api/ai-debug/agent/{sid}?app_name=blog", headers=H).status_code == 404
    calls = []
    monkeypatch.setattr(hermes_agent_router, "execute_remediation_action",
                        lambda app, action: calls.append(action.action_type) or {"status": "success"})
    res = client.post(f"/api/ai-debug/agent/{sid}/apply", headers=H, json={"app_name": "shop", "paths": ["app.js"]})
    assert res.status_code == 200 and calls == ["edit_file", "rebuild"]
