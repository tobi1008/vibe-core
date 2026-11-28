import os
import subprocess

import pytest

import ai_credentials as ac
import ai_sandbox
import builder_core
import hermes_tools
import user_directory


def test_cli_env_strips_vibe_secrets_keeps_cli_auth():
    env = {"PATH": "/bin", "VIBE_API_KEY": "x", "DOCKER_PASSWORD": "x", "GITHUB_WEBHOOK_SECRET": "x",
           "TELEGRAM_BOT_TOKEN": "x", "AI_KEYS_SECRET": "x", "INTERNAL_BEARER_TOKEN": "x", "DATABASE_URL": "x",
           "KUBECONFIG": "/root/.kube/config", "ANTHROPIC_API_KEY": "keep", "GEMINI_API_KEY": "keep", "LANG": "C"}
    clean = ai_sandbox.cli_env(env)
    assert clean["KUBECONFIG"] == "/dev/null"
    assert {k for k in clean if k != "KUBECONFIG"} == {"PATH", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LANG"}


def test_wrap_without_user_is_unchanged(monkeypatch):
    monkeypatch.delenv("AI_CLI_USER", raising=False)
    assert ai_sandbox.wrap(["/root/.local/bin/agy", "-p", "x"], {}, None) == ["/root/.local/bin/agy", "-p", "x"]


def test_wrap_with_user_uses_sudo_and_clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CLI_USER", "vibe-ai")
    monkeypatch.setattr(ai_sandbox.pwd, "getpwnam", lambda u: type("P", (), {"pw_dir": "/home/vibe-ai"})())
    cmd = ai_sandbox.wrap(["/root/.local/bin/agy", "-p", "x"], {"LANG": "C"}, str(tmp_path))
    assert cmd[:6] == ["sudo", "-n", "-u", "vibe-ai", "--", "env"]
    assert "HOME=/home/vibe-ai" in cmd and cmd[-3:] == ["agy", "-p", "x"]


def test_missing_cli_user_fails_loudly(monkeypatch):
    monkeypatch.setenv("AI_CLI_USER", "nobody-here-xyz")
    with pytest.raises(RuntimeError, match="không tồn tại"):
        ai_sandbox.wrap(["agy"], {}, None)


def test_agy_runs_with_filtered_env(monkeypatch, paths):
    monkeypatch.delenv("AI_CLI_USER", raising=False)
    monkeypatch.setenv("VIBE_API_KEY", "super-secret")
    seen = []
    monkeypatch.setattr(ai_sandbox.subprocess, "run", lambda cmd, **k: seen.append(k["env"]) or
                        subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr=""))
    builder_core.run_agy_prompt("p", app_name="shop")
    assert "VIBE_API_KEY" not in seen[0] and seen[0]["KUBECONFIG"] == "/dev/null"


DIRECTORY = {
    "users": {"u-alice": {"id": "u-alice", "username": "alice", "role": "user",
                          "package": {"name": "Free", "maxProjects": 2, "cpu": "250m", "memory": "256Mi",
                                      "allowDatabase": False, "dbStorage": "1Gi", "unlimited": False}}},
    "projects": [{"app_name": "shop", "userId": "u-alice"}],
}


def test_goi_shows_package_ai_and_permissions(monkeypatch, paths, tmp_path):
    monkeypatch.setattr(user_directory, "get_directory", lambda force=False: DIRECTORY)
    text = hermes_tools.run_tool("u-alice", "my_package", {})
    assert "Free" in text and "Project: 1/2" in text and "250m" in text and "không có trong gói" in text and "Tổng CPU" in text
    assert "AI của Hermes: Hermes (model miễn phí" in text and "KHÔNG được làm" in text
    ac.save("u-alice", "openai", "gpt-x", api_key="sk-abcdefgh1234")
    assert "Key AI riêng của bạn – OpenAI gpt-x" in hermes_tools.run_tool("u-alice", "my_package", {})


def test_agy_and_claude_run_without_tools(monkeypatch, paths):
    """AI CLI không được chạy lệnh / đọc file trên máy chủ: lỗ hổng cũ là --dangerously-skip-permissions"""
    monkeypatch.setenv("AI_CLI_USER", "0")  # không có user riêng: chạy bằng root nên phải chặn hết công cụ
    cmds = []
    monkeypatch.setattr(ai_sandbox.subprocess, "run", lambda cmd, **k: cmds.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr=""))
    builder_core.run_agy_prompt("kiểm tra dung lượng VPS", app_name="shop")
    builder_core.run_claude_prompt("kiểm tra dung lượng VPS", app_name="shop")
    agy, claude = cmds[0], cmds[1]
    assert "--dangerously-skip-permissions" not in agy and agy[-2:] == ["--mode", "plan"]
    assert "--dangerously-skip-permissions" not in claude and claude[-2:] == ["--tools", ""]
    for cmd in (agy, claude):
        prompt = cmd[cmd.index("-p") + 1]
        assert prompt.startswith("[Giới hạn] Bạn không được truy cập máy chủ") and prompt.endswith("kiểm tra dung lượng VPS")


def test_cli_runs_as_vibe_ai_in_empty_dir_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CLI_USER", "vibe-ai")
    monkeypatch.setattr(ai_sandbox, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(ai_sandbox.pwd, "getpwnam", lambda u: type("P", (), {"pw_dir": "/home/vibe-ai"})())
    seen = []

    def run(cmd, **k):
        if cmd[0] == "chown":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        seen.append((cmd, k["cwd"], os.listdir(k["cwd"])))
        return subprocess.CompletedProcess(cmd, 0, "{}", "")
    monkeypatch.setattr(ai_sandbox.subprocess, "run", run)
    assert ai_sandbox.agy_flags() == []
    ai_sandbox.run(["/root/.local/bin/agy", "-p", "x"], cwd="/opt/vibe-hosting/workspaces/shop", timeout=5, env={})
    cmd, cwd, contents = seen[0]
    assert cmd[:4] == ["sudo", "-n", "-u", "vibe-ai"] and cmd[-3:] == ["agy", "-p", "x"]
    assert cwd.startswith(str(tmp_path)) and contents == []  # thư mục trống, không phải workspace
    assert os.listdir(tmp_path) == []  # đã dọn


def test_cli_user_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AI_CLI_USER", "0")
    assert ai_sandbox.cli_user() is None and ai_sandbox.agy_flags() == ["--mode", "plan"]
