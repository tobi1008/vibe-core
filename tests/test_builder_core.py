import os
import re

import pytest

import builder_core

DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: shop
spec:
  template:
    spec:
      containers:
      - name: shop
        image: nginx:latest
"""


@pytest.fixture
def fake_cmds(monkeypatch):
    """Giả lập git/tree/nixpacks/docker/kubectl, ghi lại các lệnh đã chạy"""
    ran = []

    def fake_run_cmd(cmd, app_name, cwd=None):
        ran.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            os.makedirs(cmd[3])
            with open(os.path.join(cmd[3], "server.js"), "w") as f:
                f.write("app.listen(3000, 'localhost')")
        return ""

    monkeypatch.setattr(builder_core, "run_cmd", fake_run_cmd)
    return ran


def ai_returns(monkeypatch, config):
    monkeypatch.setattr(builder_core, "call_ai_json", lambda *a, **kw: config)


def ai_config(**overrides):
    cfg = {
        "nixpacks_toml": "[start]\ncmd = 'node server.js'",
        "k3s_deployment": DEPLOYMENT,
        "k3s_service": "kind: Service",
        "k3s_ingress": "kind: Ingress",
        "auto_patches": [],
        "user_action_required": "",
    }
    cfg.update(overrides)
    return cfg


def log_lines(paths, app):
    return [line for line in (paths.logs / f"{app}.log").read_text().splitlines() if line.strip()]


def test_deploy_app_happy_path(paths, fake_cmds, monkeypatch):
    ai_returns(monkeypatch, ai_config())
    builder_core.deploy_app("https://github.com/u/shop", "shop", "shop.example.com")

    ws = paths.ws / "shop"
    pushed = [c[2] for c in fake_cmds if c[:2] == ["docker", "push"]]
    assert len(pushed) == 1 and re.fullmatch(r"tobi1008/shop:\d{10}", pushed[0])  # tag theo thời điểm build, không dùng :latest
    assert f"image: {pushed[0]}" in (ws / "k8s" / "deployment.yaml").read_text()
    assert ["nixpacks", "build", ".", "--name", pushed[0]] in fake_cmds
    assert "nginx" not in (ws / "k8s" / "deployment.yaml").read_text()
    assert "'0.0.0.0'" in (ws / "server.js").read_text()
    assert sum(1 for c in fake_cmds if c[:2] == ["kubectl", "apply"]) == 3
    assert log_lines(paths, "shop")[-1] == "DONE"


def test_deploy_app_stops_when_ai_keeps_failing(paths, fake_cmds, monkeypatch):
    def broken(*a, **kw):
        raise ValueError("not json")

    monkeypatch.setattr(builder_core, "call_ai_json", broken)
    builder_core.deploy_app("https://github.com/u/shop", "shop", "shop.example.com")
    assert log_lines(paths, "shop")[-1] == "ERROR"
    assert not any(c[:2] == ["docker", "push"] for c in fake_cmds)


def test_deploy_app_requires_user_action(paths, fake_cmds, monkeypatch):
    ai_returns(monkeypatch, ai_config(k3s_deployment="", user_action_required="Thiếu package.json"))
    builder_core.deploy_app("https://github.com/u/shop", "shop", "shop.example.com")
    text = (paths.logs / "shop.log").read_text()
    assert "Thiếu package.json" in text
    assert log_lines(paths, "shop")[-1] == "ERROR"


def test_deploy_app_retries_build_with_error_context(paths, fake_cmds, monkeypatch):
    prompts = []

    def ai(prompt, **kw):
        prompts.append(prompt)
        return ai_config()

    builds = {"n": 0}
    base_run = builder_core.run_cmd

    def flaky(cmd, app_name, cwd=None):
        if cmd[0] == "nixpacks":
            builds["n"] += 1
            if builds["n"] == 1:
                raise Exception("npm ERR! missing script: start")
        return base_run(cmd, app_name, cwd)

    monkeypatch.setattr(builder_core, "call_ai_json", ai)
    monkeypatch.setattr(builder_core, "run_cmd", flaky)
    builder_core.deploy_app("https://github.com/u/shop", "shop", "shop.example.com")

    assert len(prompts) == 2
    assert "missing script: start" in prompts[1]
    assert log_lines(paths, "shop")[-1] == "DONE"


def test_ai_patch_cannot_escape_workspace(paths, fake_cmds, monkeypatch):
    ai_returns(monkeypatch, ai_config(auto_patches=[{"file_name": "../escaped.txt", "new_content": "pwned"}]))
    builder_core.deploy_app("https://github.com/u/shop", "shop", "shop.example.com")
    assert not (paths.ws / "escaped.txt").exists()


def test_deploy_app_errors_when_pods_never_ready(paths, fake_cmds, monkeypatch, kube):
    ai_returns(monkeypatch, ai_config())
    kube.rollout_ok = False
    kube.workloads = {"deployment/shop": {"spec": {"replicas": 1, "selector": {"matchLabels": {"app": "shop"}}}, "status": {}}}
    builder_core.deploy_app("https://github.com/u/shop", "shop", "shop.example.com")
    lines = log_lines(paths, "shop")
    assert lines[-1] == "ERROR"
    assert not any("[HOÀN TẤT]" in line for line in lines)


def test_each_deploy_gets_a_new_image_tag(monkeypatch):
    ticks = iter([1761800000, 1761800500])
    monkeypatch.setattr(builder_core.time, "time", lambda: next(ticks))
    assert builder_core.new_image_tag("tobi1008/shop") == "tobi1008/shop:1761800000"
    assert builder_core.new_image_tag("tobi1008/shop-be") == "tobi1008/shop-be:1761800500"


# ---------- Web tĩnh ----------

def test_static_site_ignores_ai_start_command(tmp_path, paths):
    # Tình huống thực tế (app "nutkin"): AI sinh `npx serve` cho web tĩnh, image chỉ có nginx => crash
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "assets").mkdir()
    toml = builder_core.resolve_nixpacks_toml(str(tmp_path), '[start]\ncmd = "npx --yes serve -s . -l 80"', "nutkin")
    assert toml == builder_core.STATIC_SITE_NIXPACKS_TOML
    assert "npx" not in toml and "[start]" not in toml


@pytest.mark.parametrize("marker", ["package.json", "requirements.txt", "Dockerfile", "index.php"])
def test_site_with_runtime_keeps_ai_config(tmp_path, paths, marker):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / marker).write_text("")
    ai_toml = '[start]\ncmd = "npm start"'
    assert builder_core.resolve_nixpacks_toml(str(tmp_path), ai_toml, "app") == ai_toml
