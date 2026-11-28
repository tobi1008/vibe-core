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
    granted = []
    monkeypatch.setattr(ai_sandbox, "_grant_read", lambda path, user: granted.append((path, user)))
    cmd = ai_sandbox.wrap(["/root/.local/bin/agy", "-p", "x"], {"LANG": "C"}, str(tmp_path))
    assert cmd[:6] == ["sudo", "-n", "-u", "vibe-ai", "--", "env"]
    assert "HOME=/home/vibe-ai" in cmd and cmd[-3:] == ["agy", "-p", "x"]
    assert granted == [(str(tmp_path), "vibe-ai")]


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
    assert "model có sẵn của hệ thống" in text and "KHÔNG được làm" in text
    ac.save("u-alice", "openai", "gpt-x", api_key="sk-abcdefgh1234")
    assert "key riêng của bạn – OpenAI gpt-x" in hermes_tools.run_tool("u-alice", "my_package", {})
