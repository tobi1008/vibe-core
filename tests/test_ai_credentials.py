import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

import ai_credentials as ac
import builder_core
import byo_ai
import hermes_butler
import main
from conftest import API_KEY

H = {"X-API-Key": API_KEY}
KEY = "sk-test-SECRET-123456789"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def http(monkeypatch):
    calls = []
    replies = {}

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        for frag, resp in replies.items():
            if frag in url:
                return resp
        return FakeResp(200, {"choices": [{"message": {"content": "OK"}}]})
    monkeypatch.setattr(byo_ai.requests, "post", post)
    return {"calls": calls, "replies": replies}


def test_key_encrypted_at_rest_and_never_returned(paths):
    view = ac.save("u1", "openai", "gpt-x", api_key=KEY)
    assert KEY not in json.dumps(view) and view["key_last4"] == "6789" and view["is_default"]
    raw = open(ac.AI_CREDENTIALS_FILE).read()
    assert KEY not in raw
    assert stat.S_IMODE(os.stat(ac.AI_CREDENTIALS_FILE).st_mode) == 0o600
    assert ac.resolve(view["engine"], "u1")["api_key"] == KEY


def test_cannot_use_or_edit_other_users_key(paths):
    view = ac.save("u1", "openai", "gpt-x", api_key=KEY)
    with pytest.raises(ac.CredentialError):
        ac.resolve(view["engine"], "u2")
    with pytest.raises(ac.CredentialError):
        ac.resolve(view["engine"], None)
    with pytest.raises(ac.CredentialError):
        ac.save("u2", "openai", "gpt-y", cred_id=view["id"])
    assert not ac.delete("u2", view["id"])
    assert ac.list_for("u2") == []


def test_compatible_requires_base_url_and_default_switch(paths):
    with pytest.raises(ac.CredentialError):
        ac.save("u1", "openai_compatible", "m", api_key=KEY)
    a = ac.save("u1", "openai_compatible", "deepseek/x", api_key=KEY, base_url="https://openrouter.ai/api/v1/")
    b = ac.save("u1", "gemini", "gemini-x", api_key=KEY, is_default=True)
    assert a["base_url"] == "https://openrouter.ai/api/v1"
    assert ac.default_engine("u1") == b["engine"]
    ac.delete("u1", b["id"])
    assert ac.default_engine("u1") == a["engine"]


def test_adapters_send_correct_requests(paths, http):
    http["replies"]["anthropic"] = FakeResp(200, {"content": [{"type": "text", "text": "hi"}]})
    http["replies"]["generateContent"] = FakeResp(200, {"candidates": [{"content": {"parts": [{"text": "yo"}]}}]})
    assert byo_ai.complete({"provider": "openai", "api_key": KEY, "model": "m"}, "p") == "OK"
    assert http["calls"][-1]["url"] == "https://api.openai.com/v1/chat/completions"
    assert http["calls"][-1]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert byo_ai.complete({"provider": "anthropic", "api_key": KEY, "model": "c"}, "p") == "hi"
    assert http["calls"][-1]["headers"]["x-api-key"] == KEY
    assert byo_ai.complete({"provider": "gemini", "api_key": KEY, "model": "g"}, "p") == "yo"
    assert http["calls"][-1]["url"].endswith("/models/g:generateContent")
    byo_ai.complete({"provider": "openai_compatible", "api_key": KEY, "model": "m", "base_url": "http://h/v1"}, "p")
    assert http["calls"][-1]["url"] == "http://h/v1/chat/completions"


def test_errors_never_leak_key(paths, http):
    http["replies"]["chat"] = FakeResp(401, {"error": {"message": f"Incorrect API key provided: {KEY}"}})
    with pytest.raises(byo_ai.ByoAIError) as e:
        byo_ai.complete({"provider": "openai", "api_key": KEY, "model": "m"}, "p")
    assert KEY not in str(e.value) and "401" in str(e.value)


def test_api_tests_before_saving_and_hides_key(paths, http):
    client = TestClient(main.app)
    http["replies"]["chat"] = FakeResp(401, {"error": {"message": "bad key"}})
    res = client.put("/api/ai-credentials", headers=H,
                     json={"user_id": "u1", "provider": "openai", "model": "m", "api_key": KEY})
    assert res.status_code == 400 and "chưa lưu" in res.json()["detail"]
    assert ac.list_for("u1") == []
    http["replies"].clear()
    res = client.put("/api/ai-credentials", headers=H,
                     json={"user_id": "u1", "provider": "openai", "model": "m", "api_key": KEY})
    assert res.status_code == 200 and KEY not in res.text
    listed = client.get("/api/ai-credentials?user_id=u1", headers=H).json()
    assert len(listed["credentials"]) == 1 and KEY not in json.dumps(listed)
    cid = listed["credentials"][0]["id"]
    assert client.delete(f"/api/ai-credentials/{cid}?user_id=u2", headers=H).status_code == 404
    assert client.delete(f"/api/ai-credentials/{cid}?user_id=u1", headers=H).status_code == 200


def test_call_ai_uses_app_owner_key(paths, http, monkeypatch):
    http["replies"]["chat"] = FakeResp(200, {"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]})
    view = ac.save("u1", "openai", "m", api_key=KEY)
    ac.register_app_owner("shop", "u1")
    assert builder_core.call_ai_json("p", app_name="shop-be", ai_provider=view["engine"]) == {"a": 1}
    ac.register_app_owner("shop", "u2")  # app thuộc người khác: không được dùng key của u1
    with pytest.raises(Exception, match="không thuộc"):
        builder_core.call_ai_prompt("p", app_name="shop", ai_provider=view["engine"])


def test_deploy_rejects_key_not_owned_by_app_owner(paths, monkeypatch):
    view = ac.save("u1", "openai", "m", api_key=KEY)
    monkeypatch.setattr(main, "deploy_app", lambda *a, **k: None)
    client = TestClient(main.app)
    body = {"repo_url": "https://github.com/a/b", "app_name": "shop", "app_domain": "shop.vibe.quyenlt.com",
            "ai_provider": view["engine"]}
    assert client.post("/api/deploy", json=body, headers=H).status_code == 403
    assert client.post("/api/apps/shop/owner", json={"user_id": "u1"}, headers=H).status_code == 200
    res = client.post("/api/deploy", json=body, headers=H)
    assert res.status_code == 200
    assert main.get_app_config("shop")["ai_provider"] == view["engine"]


def test_hermes_telegram_uses_users_default_key(paths, monkeypatch):
    used = []
    monkeypatch.setattr(builder_core, "call_ai_prompt",
                        lambda prompt, **k: used.append(k) or '{"reply": "chào"}')
    assert hermes_butler.llm_for("u9") is hermes_butler.call_llm
    view = ac.save("u9", "anthropic", "c", api_key=KEY)
    hermes_butler.llm_for("u9")("hello")
    assert used[-1]["ai_provider"] == view["engine"] and used[-1]["owner_user_id"] == "u9"
