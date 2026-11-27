"""
Kiểm thử các hàm gọi AI trong builder_core: Antigravity CLI (agy), Claude Code CLI và Hermes API.
Không gọi AI thật: subprocess.run và requests.post được giả lập.
"""

import json
import subprocess

import pytest

import builder_core


class Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Resp:
    def __init__(self, status_code=200, content="", text=""):
        self.status_code, self._content, self.text = status_code, content, text

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.fixture
def logs(monkeypatch):
    lines = []
    monkeypatch.setattr(builder_core, "write_log", lambda app, msg: lines.append(msg))
    return lines


def fake_subprocess(monkeypatch, outcomes):
    """outcomes: danh sách Res hoặc Exception, trả lần lượt cho mỗi lần gọi subprocess.run"""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(builder_core.subprocess, "run", run)
    return calls


# ---------- Antigravity CLI ----------

def test_agy_first_model_success(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_agy_bin", lambda: "agy")
    calls = fake_subprocess(monkeypatch, [Res(0, "  xin chao  ")])
    assert builder_core.run_agy_prompt("hi", app_name="a") == "xin chao"
    assert calls[0][:4] == ["agy", "-p", "hi", "--model"]
    assert calls[0][4] == builder_core.AGY_MODELS[0]


def test_agy_preferred_model_is_tried_first(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_agy_bin", lambda: "agy")
    calls = fake_subprocess(monkeypatch, [Res(0, "ok")])
    preferred = builder_core.AGY_MODELS[-1]
    builder_core.run_agy_prompt("hi", preferred_model=preferred)
    assert calls[0][4] == preferred


def test_agy_falls_back_through_models(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_agy_bin", lambda: "agy")
    outcomes = [Res(1, "", "quota"), subprocess.TimeoutExpired("agy", 5), RuntimeError("boom")]
    outcomes += [Res(0, "ok")] * 10
    calls = fake_subprocess(monkeypatch, outcomes)
    assert builder_core.run_agy_prompt("hi") == "ok"
    assert len(calls) == 4
    assert any("quota" in line for line in logs)
    assert any("quá thời gian chờ" in line for line in logs)


def test_agy_default_model_fallback(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_agy_bin", lambda: "agy")
    n = len(builder_core.AGY_MODELS)
    calls = fake_subprocess(monkeypatch, [Res(1, "", "err")] * n + [Res(0, "mac dinh")])
    assert builder_core.run_agy_prompt("hi") == "mac dinh"
    assert "--model" not in calls[-1]


def test_agy_all_fail_raises(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_agy_bin", lambda: "agy")
    fake_subprocess(monkeypatch, [Res(1, "", "het quota")])
    with pytest.raises(Exception, match="Antigravity"):
        builder_core.run_agy_prompt("hi")


def test_agy_long_prompt_is_truncated(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_agy_bin", lambda: "agy")
    calls = fake_subprocess(monkeypatch, [Res(0, "ok")])
    builder_core.run_agy_prompt("x" * 80000)
    assert len(calls[0][2]) < 71000


@pytest.mark.parametrize("output", [
    'Ket qua:\n```json\n{"a": 1}\n```',
    'truoc {"a": 1} sau',
    '\x1b[32m{"a": 1}\x1b[0m',
])
def test_agy_json_extracts_object(monkeypatch, logs, output):
    monkeypatch.setattr(builder_core, "run_agy_prompt", lambda *a, **kw: output)
    assert builder_core.run_agy_json("p") == {"a": 1}


def test_agy_json_invalid_raises_and_logs(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "run_agy_prompt", lambda *a, **kw: "khong co json")
    with pytest.raises(json.JSONDecodeError):
        builder_core.run_agy_json("p")
    assert any("LỖI JSON AGY" in line for line in logs)


def test_get_agy_bin_from_env(monkeypatch, tmp_path):
    fake = tmp_path / "agy"
    fake.write_text("")
    monkeypatch.setenv("AGY_BIN_PATH", str(fake))
    assert builder_core.get_agy_bin() == str(fake)


def test_get_agy_bin_from_path(monkeypatch):
    monkeypatch.delenv("AGY_BIN_PATH", raising=False)
    monkeypatch.setattr(builder_core.shutil, "which", lambda name: "/x/agy")
    assert builder_core.get_agy_bin() == "/x/agy"


def test_get_agy_bin_default(monkeypatch):
    monkeypatch.delenv("AGY_BIN_PATH", raising=False)
    monkeypatch.setattr(builder_core.shutil, "which", lambda name: None)
    monkeypatch.setattr(builder_core.os.path, "exists", lambda p: False)
    assert builder_core.get_agy_bin() == "agy"


# ---------- Claude Code CLI ----------

def test_claude_first_model_success(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_claude_bin", lambda: "claude")
    calls = fake_subprocess(monkeypatch, [Res(0, "ok")])
    assert builder_core.run_claude_prompt("hi") == "ok"
    assert calls[0][:3] == ["claude", "-p", "hi"]


def test_claude_falls_back_then_default(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_claude_bin", lambda: "claude")
    n = len(builder_core.CLAUDE_MODELS)
    errors = [Res(1, "", "loi"), subprocess.TimeoutExpired("claude", 5), RuntimeError("x")]
    outcomes = (errors * n)[:n] + [Res(0, "mac dinh")]
    calls = fake_subprocess(monkeypatch, outcomes)
    assert builder_core.run_claude_prompt("x" * 80000) == "mac dinh"
    assert calls[-1] == ["claude", "-p", calls[-1][2]]
    assert len(calls[-1][2]) < 71000


def test_claude_all_fail_raises(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "get_claude_bin", lambda: "claude")
    fake_subprocess(monkeypatch, [Res(1, "", "loi")])
    with pytest.raises(Exception, match="Claude Code"):
        builder_core.run_claude_prompt("hi")


def test_claude_json_from_markdown(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "run_claude_prompt", lambda *a, **kw: '```json\n{"ok": true}\n```')
    assert builder_core.run_claude_json("p") == {"ok": True}


def test_claude_json_plain_object(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "run_claude_prompt", lambda *a, **kw: 'xem: {"b": "c\\d"}')
    assert builder_core.run_claude_json("p") == {"b": "c\\d"}


def test_claude_json_invalid_raises(monkeypatch, logs):
    monkeypatch.setattr(builder_core, "run_claude_prompt", lambda *a, **kw: "``` khong phai json ```")
    with pytest.raises(json.JSONDecodeError):
        builder_core.run_claude_json("p")
    assert any("LỖI JSON CLAUDE" in line for line in logs)


def test_get_claude_bin_from_env(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("")
    monkeypatch.setenv("CLAUDE_BIN_PATH", str(fake))
    assert builder_core.get_claude_bin() == str(fake)


def test_get_claude_bin_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_BIN_PATH", raising=False)
    monkeypatch.setattr(builder_core.shutil, "which", lambda name: None)
    monkeypatch.setattr(builder_core.os.path, "exists", lambda p: False)
    assert builder_core.get_claude_bin() == "claude"


# ---------- Hermes API ----------

def test_hermes_success(monkeypatch, logs):
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append(dict(json))
        return Resp(200, "  tra loi  ")

    monkeypatch.setattr(builder_core.requests, "post", post)
    assert builder_core.run_hermes_prompt("hi") == "tra loi"
    assert sent[0]["model"] == builder_core.HERMES_MODELS[0]
    assert sent[0]["messages"][0]["content"] == "hi"


def test_hermes_fallback_on_429_error_and_exception(monkeypatch, logs):
    seq = iter([Resp(429), Resp(500, text="loi may chu"), RuntimeError("mat ket noi"), Resp(200, "ok")])

    def post(*a, **kw):
        r = next(seq)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(builder_core.requests, "post", post)
    assert builder_core.run_hermes_prompt("hi") == "ok"
    assert any("Rate Limit" in line for line in logs)
    assert any("500" in line for line in logs)


def test_hermes_max_models_limits_attempts(monkeypatch, logs):
    count = []

    def post(*a, **kw):
        count.append(1)
        return Resp(503, text="ban")

    monkeypatch.setattr(builder_core.requests, "post", post)
    with pytest.raises(Exception, match="Hermes"):
        builder_core.run_hermes_prompt("hi", max_models=2)
    assert len(count) == 2


def test_hermes_preferred_model_first(monkeypatch, logs):
    models = []

    def post(url, headers=None, json=None, timeout=None):
        models.append(json["model"])
        return Resp(200, "ok")

    monkeypatch.setattr(builder_core.requests, "post", post)
    builder_core.run_hermes_prompt("hi", preferred_model=builder_core.HERMES_MODELS[-1])
    assert models == [builder_core.HERMES_MODELS[-1]]


@pytest.mark.parametrize("output,expected", [
    ('ket qua {"x": 2} het', {"x": 2}),
    ('```json\n[1, 2]\n```', [1, 2]),
])
def test_hermes_json(monkeypatch, output, expected):
    monkeypatch.setattr(builder_core, "run_hermes_prompt", lambda *a, **kw: output)
    assert builder_core.run_hermes_json("p") == expected


# ---------- Bộ điều phối ----------

@pytest.mark.parametrize("provider,target", [
    ("claude", "run_claude_json"), ("Claude-Code", "run_claude_json"),
    ("hermes", "run_hermes_json"), (None, "run_agy_json"), ("agy", "run_agy_json"),
])
def test_call_ai_json_dispatch(monkeypatch, provider, target):
    called = []
    for name in ("run_claude_json", "run_hermes_json", "run_agy_json"):
        monkeypatch.setattr(builder_core, name, lambda *a, _n=name, **kw: called.append((_n, kw.get("timeout"))) or {})
    builder_core.call_ai_json("p", ai_provider=provider)
    assert called[0][0] == target
    assert called[0][1] in (75, 90)


@pytest.mark.parametrize("provider,target", [
    ("anthropic", "run_claude_prompt"), ("hermes", "run_hermes_prompt"), ("", "run_agy_prompt"),
])
def test_call_ai_prompt_dispatch(monkeypatch, provider, target):
    called = []
    for name in ("run_claude_prompt", "run_hermes_prompt", "run_agy_prompt"):
        monkeypatch.setattr(builder_core, name, lambda *a, _n=name, **kw: called.append(_n) or "ok")
    assert builder_core.call_ai_prompt("p", ai_provider=provider, timeout=10) == "ok"
    assert called == [target]
