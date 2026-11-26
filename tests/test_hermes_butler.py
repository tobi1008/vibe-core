"""
Kiểm thử Hermes Butler (bot Telegram), bộ công cụ, liên kết tài khoản và Hermes Monitor.
Không gọi Telegram, frontend hay LLM thật: các điểm gọi ra ngoài đều được giả lập.
"""

import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import hermes_butler
import hermes_monitor
import hermes_tools
import main
import telegram_api
import telegram_client
import telegram_links
import user_directory
from conftest import API_KEY

DIRECTORY = {
    "users": {
        "u-alice": {"id": "u-alice", "username": "alice", "role": "user"},
        "u-bob": {"id": "u-bob", "username": "bob", "role": "user"},
        "u-admin": {"id": "u-admin", "username": "admin", "role": "admin"},
    },
    "projects": [
        {"app_name": "shop", "userId": "u-alice"},
        {"app_name": "blog", "userId": "u-bob"},
    ],
}


class SyncExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def env(tmp_path, monkeypatch, paths):
    """Liên kết lưu ở thư mục tạm, danh bạ người dùng giả, tin Telegram gửi vào danh sách `sent`"""
    monkeypatch.setattr(telegram_links, "LINKS_FILE", str(tmp_path / "links.json"))
    monkeypatch.setattr(hermes_monitor, "STATE_FILE", str(tmp_path / "monitor.json"))
    monkeypatch.setattr(user_directory, "get_directory", lambda force=False: DIRECTORY)
    monkeypatch.setattr(hermes_tools, "all_app_names", lambda: ["blog", "shop"])
    monkeypatch.setattr(hermes_tools, "app_domains", lambda app: [f"{app}.vibe.quyenlt.com"])
    sent = []
    monkeypatch.setattr(telegram_client, "send_message",
                        lambda chat_id, text, reply_markup=None: sent.append((chat_id, text, reply_markup)))
    monkeypatch.setattr(telegram_client, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(telegram_client, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(telegram_client, "clear_keyboard", lambda *a, **k: None)
    hermes_butler._approvals.clear()
    bot = hermes_butler.Butler()
    bot.executor = SyncExecutor()
    return {"sent": sent, "bot": bot, "tmp": tmp_path}


def link(chat_id: int, user_id: str) -> None:
    code = telegram_links.create_link_code(user_id, user_id)["code"]
    assert telegram_links.consume_link_code(code, chat_id, "tg")


def private_message(chat_id: int, text: str) -> dict:
    return {"chat": {"id": chat_id, "type": "private"}, "from": {"username": "tg"}, "text": text}


# ─────────────────────────── Liên kết tài khoản ───────────────────────────

def test_link_code_is_single_use(env):
    code = telegram_links.create_link_code("u-alice", "alice")["code"]
    assert telegram_links.consume_link_code(code, 111, "alice_tg")["user_id"] == "u-alice"
    assert telegram_links.consume_link_code(code, 222, "other") is None
    assert telegram_links.get_link(111)["user_id"] == "u-alice"
    assert telegram_links.get_link(222) is None


def test_link_code_expires(env, monkeypatch):
    code = telegram_links.create_link_code("u-alice")["code"]
    real_time = time.time
    monkeypatch.setattr(telegram_links.time, "time", lambda: real_time() + telegram_links.LINK_CODE_TTL + 1)
    assert telegram_links.consume_link_code(code, 111) is None


def test_new_code_invalidates_previous_code(env):
    old = telegram_links.create_link_code("u-alice")["code"]
    new = telegram_links.create_link_code("u-alice")["code"]
    assert telegram_links.consume_link_code(old, 111) is None
    assert telegram_links.consume_link_code(new, 111) is not None


def test_unlink_user_removes_all_chats(env):
    link(111, "u-alice")
    link(112, "u-alice")
    link(222, "u-bob")
    assert telegram_links.unlink_user("u-alice") == 2
    assert telegram_links.chats_for_user("u-alice") == []
    assert telegram_links.chats_for_user("u-bob") == [222]


# ─────────────────────────── Quyền sở hữu ───────────────────────────

def test_owner_can_access_own_app_and_components(env):
    assert user_directory.can_access("u-alice", "shop")
    assert user_directory.can_access("u-alice", "shop-be")
    assert not user_directory.can_access("u-alice", "blog")


def test_admin_can_access_every_app(env):
    assert user_directory.can_access("u-admin", "blog")
    assert hermes_tools.visible_apps("u-admin") == ["blog", "shop"]
    assert hermes_tools.visible_apps("u-alice") == ["shop"]


def test_unknown_user_has_no_access(env):
    assert not user_directory.can_access("u-ghost", "shop")


def test_directory_down_means_no_access(env, monkeypatch):
    def down(force=False):
        raise user_directory.DirectoryUnavailable("frontend lỗi")
    monkeypatch.setattr(user_directory, "get_directory", down)
    assert not user_directory.can_access("u-admin", "shop")


@pytest.mark.parametrize("app", ["blog", "../etc", "Shop App", "", None])
def test_check_access_rejects_foreign_or_invalid_app(env, app):
    with pytest.raises(hermes_tools.ToolError):
        hermes_tools.check_access("u-alice", app)


# ─────────────────────────── Vòng lặp tác tử ───────────────────────────

def scripted_llm(*responses):
    queue = list(responses)
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return queue.pop(0)
    llm.prompts = prompts
    return llm


def test_parse_decision_handles_fences_and_plain_text():
    assert hermes_butler.parse_decision('```json\n{"tool": "app_status", "args": {"app": "shop"}}\n```')["tool"] == "app_status"
    assert hermes_butler.parse_decision("App đang chạy bình thường.") == {"reply": "App đang chạy bình thường."}
    assert hermes_butler.parse_decision('{"tool": "list_apps"}')["args"] == {}


def test_agent_runs_read_tool_then_replies(env, monkeypatch):
    calls = []
    monkeypatch.setitem(hermes_tools.TOOLS["app_status"], "fn", lambda uid, args: calls.append(args) or "shop: 0/1 Ready, CrashLoopBackOff")
    llm = scripted_llm('{"tool": "app_status", "args": {"app": "shop"}}', '{"reply": "shop đang crash."}')
    result = hermes_butler.run_agent(111, "u-alice", [], "shop sao rồi?", llm=llm)
    assert result == {"reply": "shop đang crash.", "approvals": []}
    assert calls == [{"app": "shop"}]
    assert "CrashLoopBackOff" in llm.prompts[1]


def test_agent_never_executes_mutating_tool_directly(env, monkeypatch):
    executed = []
    monkeypatch.setitem(hermes_tools.TOOLS["restart_app"], "fn", lambda uid, args: executed.append(args) or "ok")
    llm = scripted_llm('{"tool": "restart_app", "args": {"app": "shop"}, "reason": "Pod treo."}')
    result = hermes_butler.run_agent(111, "u-alice", [], "khởi động lại shop", llm=llm)
    assert executed == []
    assert len(result["approvals"]) == 1
    assert result["approvals"][0]["tool"] == "restart_app"
    assert "Pod treo." in result["reply"]


def test_agent_cannot_request_action_on_foreign_app(env):
    llm = scripted_llm('{"tool": "restart_app", "args": {"app": "blog"}}', '{"reply": "Bạn không có quyền với blog."}')
    result = hermes_butler.run_agent(111, "u-alice", [], "restart blog", llm=llm)
    assert result["approvals"] == []
    assert "không có quyền" in llm.prompts[1]


def test_agent_reports_unknown_tool_back_to_llm(env):
    llm = scripted_llm('{"tool": "rm_rf", "args": {}}', '{"reply": "Xin lỗi."}')
    result = hermes_butler.run_agent(111, "u-alice", [], "xóa hết", llm=llm)
    assert result["reply"] == "Xin lỗi."
    assert "không có công cụ này" in llm.prompts[1]


def test_agent_stops_after_max_steps(env, monkeypatch):
    monkeypatch.setitem(hermes_tools.TOOLS["list_apps"], "fn", lambda uid, args: "shop")
    llm = scripted_llm(*['{"tool": "list_apps", "args": {}}'] * hermes_butler.MAX_STEPS)
    result = hermes_butler.run_agent(111, "u-alice", [], "?", llm=llm)
    assert "chưa đi đến kết luận" in result["reply"]
    assert len(llm.prompts) == hermes_butler.MAX_STEPS


def test_agent_handles_llm_outage(env):
    def broken(prompt):
        raise Exception("Tất cả các Model Hermes đều không phản hồi")
    result = hermes_butler.run_agent(111, "u-alice", [], "hello", llm=broken)
    assert "không phản hồi" in result["reply"]


def test_diagnose_turns_each_action_into_an_approval(env, monkeypatch):
    monkeypatch.setattr(hermes_tools, "diagnose", lambda uid, args: {
        "app": "shop", "diagnosis": "Thiếu DB_HOST", "root_cause": "DB_CONFIG",
        "actions": [{"action_type": "set_env", "env_vars": {"DB_HOST": "shop-db-svc"}, "description": "Bơm DB_HOST"},
                    {"action_type": "restart_deployment", "target": "shop", "description": "Khởi động lại"}],
    })
    llm = scripted_llm('{"tool": "diagnose", "args": {"app": "shop"}}')
    result = hermes_butler.run_agent(111, "u-alice", [], "shop lỗi DB", llm=llm)
    assert [a["tool"] for a in result["approvals"]] == ["apply_fix", "apply_fix"]
    assert "Thiếu DB_HOST" in result["reply"]
    assert "DB_HOST=…" in result["approvals"][0]["description"]
    assert "shop-db-svc" not in result["approvals"][0]["description"]


# ─────────────────────────── Bot: tin nhắn và nút duyệt ───────────────────────────

def test_unlinked_chat_gets_instructions(env):
    env["bot"].handle_message(private_message(999, "app của tôi sao rồi"))
    assert env["sent"][-1][1] == hermes_butler.NOT_LINKED_TEXT


def test_group_chat_is_ignored(env):
    env["bot"].handle_message({"chat": {"id": -5, "type": "group"}, "text": "/start abc"})
    assert env["sent"] == []


def test_start_with_code_links_chat(env):
    code = telegram_links.create_link_code("u-alice", "alice")["code"]
    env["bot"].handle_message(private_message(111, f"/start {code}"))
    assert telegram_links.get_link(111)["user_id"] == "u-alice"
    assert "Đã liên kết" in env["sent"][-1][1]


def test_start_with_bad_code(env):
    env["bot"].handle_message(private_message(111, "/start saima"))
    assert telegram_links.get_link(111) is None
    assert "không đúng hoặc đã hết hạn" in env["sent"][-1][1]


def test_status_command_runs_tool_for_owner_only(env, monkeypatch):
    link(111, "u-alice")
    monkeypatch.setattr(hermes_tools.hermes_verifier, "discover_workloads", lambda app: [])
    env["bot"].handle_message(private_message(111, "/status shop"))
    assert "Không có Deployment" in env["sent"][-1][1]
    env["bot"].handle_message(private_message(111, "/status blog"))
    assert "không có quyền" in env["sent"][-1][1]


def callback(chat_id: int, data: str) -> dict:
    return {"id": "cb1", "data": data, "message": {"message_id": 7, "chat": {"id": chat_id}}}


def test_approve_executes_action_once(env, monkeypatch):
    link(111, "u-alice")
    executed = []
    monkeypatch.setitem(hermes_tools.TOOLS["restart_app"], "fn", lambda uid, args: executed.append((uid, args)) or "Đã khởi động lại shop.")
    approval = hermes_butler.create_approval(111, "u-alice", "restart_app", {"app": "shop"})
    env["bot"].handle_callback(callback(111, f"ap:{approval['id']}"))
    env["bot"].handle_callback(callback(111, f"ap:{approval['id']}"))
    assert executed == [("u-alice", {"app": "shop"})]
    assert "không còn hiệu lực" in env["sent"][-1][1]


def test_other_chat_cannot_approve(env, monkeypatch):
    link(111, "u-alice")
    link(222, "u-bob")
    executed = []
    monkeypatch.setitem(hermes_tools.TOOLS["restart_app"], "fn", lambda uid, args: executed.append(args) or "ok")
    approval = hermes_butler.create_approval(111, "u-alice", "restart_app", {"app": "shop"})
    env["bot"].handle_callback(callback(222, f"ap:{approval['id']}"))
    assert executed == []
    assert "không phải người nhận" in env["sent"][-1][1]


def test_reject_does_not_execute(env, monkeypatch):
    link(111, "u-alice")
    executed = []
    monkeypatch.setitem(hermes_tools.TOOLS["restart_app"], "fn", lambda uid, args: executed.append(args) or "ok")
    approval = hermes_butler.create_approval(111, "u-alice", "restart_app", {"app": "shop"})
    env["bot"].handle_callback(callback(111, f"rj:{approval['id']}"))
    assert executed == []
    assert "Đã bỏ qua" in env["sent"][-1][1]


def test_approval_rechecks_ownership_at_execution(env, monkeypatch):
    """Quyền bị thu hồi sau khi đề xuất ⇒ bấm Duyệt cũng không thực thi"""
    link(111, "u-alice")
    approval = hermes_butler.create_approval(111, "u-alice", "restart_app", {"app": "shop"})
    no_projects = {"users": DIRECTORY["users"], "projects": []}
    monkeypatch.setattr(user_directory, "get_directory", lambda force=False: no_projects)
    env["bot"].handle_callback(callback(111, f"ap:{approval['id']}"))
    assert "không có quyền" in env["sent"][-1][1]


def test_expired_approval_is_rejected(env, monkeypatch):
    link(111, "u-alice")
    approval = hermes_butler.create_approval(111, "u-alice", "restart_app", {"app": "shop"})
    approval["created_at"] -= hermes_butler.APPROVAL_TTL + 1
    env["bot"].handle_callback(callback(111, f"ap:{approval['id']}"))
    assert "quá 30 phút" in env["sent"][-1][1]


def test_apply_fix_cannot_be_called_by_llm():
    assert hermes_tools.get_tool("apply_fix") is None
    assert hermes_tools.get_tool("apply_fix", include_internal=True) is not None


def test_split_long_message():
    parts = telegram_client.split_message("dòng\n" * 2000, limit=500)
    assert len(parts) > 1
    assert all(len(p) <= 520 for p in parts)
    assert parts[-1].endswith(f"({len(parts)}/{len(parts)})")


# ─────────────────────────── Hermes Monitor ───────────────────────────

def test_alert_only_after_consecutive_failures_and_once(env):
    mon = hermes_monitor.Monitor()
    bad = {"level": "bad", "detail": "deployment/shop: CrashLoopBackOff"}
    assert mon.update_check("shop", "pod:deployment/shop", bad) == []
    events = mon.update_check("shop", "pod:deployment/shop", bad)
    assert [e["type"] for e in events] == ["alert"]
    assert mon.update_check("shop", "pod:deployment/shop", bad) == []
    events = mon.update_check("shop", "pod:deployment/shop", {"level": "ok", "detail": "1/1 Ready"})
    assert [e["type"] for e in events] == ["recovered"]


def test_single_failure_then_recovery_is_silent(env):
    mon = hermes_monitor.Monitor()
    mon.update_check("shop", "http:shop", {"level": "bad", "detail": "HTTP 502"})
    assert mon.update_check("shop", "http:shop", {"level": "ok", "detail": "HTTP 200"}) == []


def test_restart_increase_is_reported(env):
    mon = hermes_monitor.Monitor()
    mon.update_check("shop", "pod:deployment/shop", {"level": "ok", "detail": "1/1 Ready", "restarts": 1})
    events = mon.update_check("shop", "pod:deployment/shop", {"level": "ok", "detail": "1/1 Ready", "restarts": 3})
    assert events[0]["type"] == "restart"
    assert "2 lần" in events[0]["detail"]


def test_evaluate_workload_levels():
    crashing = {"workload": "deployment/shop", "found": True, "replicas": 1, "ready_replicas": 0,
                "pods": [{"phase": "Running", "containers": [{"ready": False, "state": "waiting", "reason": "CrashLoopBackOff", "restarts": 4}]}]}
    starting = {"workload": "deployment/shop", "found": True, "replicas": 1, "ready_replicas": 0,
                "pods": [{"phase": "Pending", "containers": [{"ready": False, "state": "waiting", "reason": "ContainerCreating", "restarts": 0}]}]}
    healthy = {"workload": "deployment/shop", "found": True, "replicas": 1, "ready_replicas": 1,
               "pods": [{"phase": "Running", "containers": [{"ready": True, "state": "running", "restarts": 0}]}]}
    assert hermes_monitor.evaluate_workload(crashing)["level"] == "bad"
    assert hermes_monitor.evaluate_workload(starting)["level"] == "warn"
    assert hermes_monitor.evaluate_workload(healthy)["level"] == "ok"


def test_alert_recipients_owner_and_admin_vps_admin_only(env):
    link(111, "u-alice")
    link(222, "u-bob")
    link(333, "u-admin")
    assert hermes_monitor.Monitor.recipients("shop") == [111, 333]
    assert hermes_monitor.Monitor.recipients("vps") == [333]


def test_notify_sends_diagnose_button_for_app_alerts(env):
    link(111, "u-alice")
    mon = hermes_monitor.Monitor()
    mon.notify([{"type": "alert", "scope": "shop", "level": "bad", "detail": "HTTP 502"}])
    chat_id, text, markup = env["sent"][-1]
    assert chat_id == 111 and "HTTP 502" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "dg:shop"


def test_check_once_skips_app_being_deployed(env, monkeypatch):
    checked = []
    monkeypatch.setattr(hermes_monitor, "recently_deploying", lambda app: app == "shop")
    monkeypatch.setattr(hermes_monitor, "check_app", lambda app: checked.append(app) or {})
    monkeypatch.setattr(hermes_monitor, "check_manager_node", lambda: {})
    monkeypatch.setattr(hermes_monitor, "check_k3s_nodes", lambda: {})
    hermes_monitor.Monitor().check_once()
    assert checked == ["blog"]


def test_daily_report_once_per_day(env):
    mon = hermes_monitor.Monitor()
    morning = datetime(2026, 11, 27, 8, 5, tzinfo=hermes_monitor.TZ)
    assert not mon.daily_report_due(datetime(2026, 11, 27, 7, 0, tzinfo=hermes_monitor.TZ))
    assert mon.daily_report_due(morning)
    mon.state["last_daily"] = "2026-11-27"
    assert not mon.daily_report_due(morning)


def test_daily_report_content_per_user(env):
    mon = hermes_monitor.Monitor()
    mon.latest = {"shop": {"pod:deployment/shop": {"level": "bad", "detail": "deployment/shop: CrashLoopBackOff"}},
                  "blog": {"pod:deployment/blog": {"level": "ok", "detail": "1/1 Ready"}},
                  "vps": {"vps:manager:disk": {"level": "ok", "detail": "Nút quản lý – ổ đĩa 40%"}}}
    alice = mon.daily_report_for("u-alice")
    assert "🔴 shop" in alice and "blog" not in alice and "ổ đĩa" not in alice
    admin = mon.daily_report_for("u-admin")
    assert "🟢 blog" in admin and "ổ đĩa 40%" in admin


# ─────────────────────────── API liên kết ───────────────────────────

@pytest.fixture
def client():
    return TestClient(main.app)


def test_link_code_api_requires_bot_configured(env, client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    res = client.post("/api/telegram/link-code", json={"user_id": "u-alice"}, headers={"X-API-Key": API_KEY})
    assert res.status_code == 503


def test_link_code_api_returns_deep_link(env, client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "vibe_hermes_bot")
    monkeypatch.setattr(telegram_api, "_bot_username", None)
    res = client.post("/api/telegram/link-code", json={"user_id": "u-alice", "username": "alice"}, headers={"X-API-Key": API_KEY})
    body = res.json()
    assert res.status_code == 200
    assert body["link"] == f"https://t.me/vibe_hermes_bot?start={body['code']}"
    status = client.get("/api/telegram/status", params={"user_id": "u-alice"}, headers={"X-API-Key": API_KEY}).json()
    assert status["linked"] is False


def test_link_api_requires_api_key(env, client):
    assert client.get("/api/telegram/status", params={"user_id": "u-alice"}).status_code == 401


def test_unlink_api(env, client):
    link(111, "u-alice")
    res = client.delete("/api/telegram/link", params={"user_id": "u-alice"}, headers={"X-API-Key": API_KEY})
    assert res.json()["removed"] == 1
    assert telegram_links.get_link(111) is None


def test_links_file_is_valid_json(env):
    link(111, "u-alice")
    data = json.loads((env["tmp"] / "links.json").read_text(encoding="utf-8"))
    assert "111" in data["chats"]


# ─────────────────────────── Công cụ K3s (giả lập kubectl) ───────────────────────────

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def kubectl(monkeypatch):
    calls = []
    responses = {}

    def fake(args, timeout=60):
        calls.append(args)
        return responses.get(args[0], FakeProc())
    monkeypatch.setattr(hermes_tools, "_kubectl", fake)
    return {"calls": calls, "responses": responses}


def deployments(monkeypatch, names):
    monkeypatch.setattr(hermes_tools.hermes_verifier, "discover_workloads",
                        lambda app: [f"deployment/{n}" for n in names])


def test_target_deployment_rules(env, monkeypatch):
    deployments(monkeypatch, ["shop-be", "shop-fe"])
    assert hermes_tools.target_deployment("shop", "fe") == "shop-fe"
    with pytest.raises(hermes_tools.ToolError, match="nhiều thành phần"):
        hermes_tools.target_deployment("shop", None)
    deployments(monkeypatch, ["shop"])
    assert hermes_tools.target_deployment("shop", None) == "shop"
    with pytest.raises(hermes_tools.ToolError, match="Không có Deployment 'shop-be'"):
        hermes_tools.target_deployment("shop", "be")


def test_invalid_component_rejected(env, monkeypatch):
    deployments(monkeypatch, ["shop"])
    with pytest.raises(hermes_tools.ToolError, match="component"):
        hermes_tools.run_tool("u-alice", "runtime_logs", {"app": "shop", "component": "db; rm -rf /"})


def test_rollback_builds_kubectl_args(env, monkeypatch, kubectl):
    deployments(monkeypatch, ["shop"])
    assert "revision 3" in hermes_tools.run_tool("u-alice", "rollback_app", {"app": "shop", "revision": 3})
    assert kubectl["calls"][-1] == ["rollout", "undo", "deployment/shop", "--to-revision=3"]
    hermes_tools.run_tool("u-alice", "rollback_app", {"app": "shop"})
    assert kubectl["calls"][-1] == ["rollout", "undo", "deployment/shop"]


def test_restart_failure_is_reported(env, monkeypatch, kubectl):
    deployments(monkeypatch, ["shop"])
    kubectl["responses"]["rollout"] = FakeProc(1, stderr="deployments.apps \"shop\" not found")
    with pytest.raises(hermes_tools.ToolError, match="not found"):
        hermes_tools.run_tool("u-alice", "restart_app", {"app": "shop"})


def test_runtime_logs_clamps_lines_and_adds_previous_crash(env, monkeypatch, kubectl):
    deployments(monkeypatch, ["shop"])
    kubectl["responses"]["logs"] = FakeProc(0, stdout="Error: listen EADDRINUSE token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    out = hermes_tools.run_tool("u-alice", "runtime_logs", {"app": "shop", "lines": 99999})
    assert "--tail=300" in kubectl["calls"][0]
    assert "LOG LẦN CRASH TRƯỚC" in out
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out


def test_app_status_summarizes_pods(env, monkeypatch):
    deployments(monkeypatch, ["shop"])
    monkeypatch.setattr(hermes_tools.hermes_verifier, "collect_workload", lambda w, with_logs=True: {
        "workload": w, "found": True, "replicas": 1, "ready_replicas": 0, "revision": "4",
        "pods": [{"name": "shop-abc", "phase": "Running", "containers": [
            {"name": "shop", "state": "waiting", "reason": "CrashLoopBackOff", "restarts": 5,
             "last_terminated": {"reason": "Error", "exit_code": 1}}]}]})
    out = hermes_tools.run_tool("u-alice", "app_status", {"app": "shop"})
    assert "0/1 Ready" in out and "CrashLoopBackOff" in out and "restart 5 lần" in out


def test_http_check_reports_errors(env, monkeypatch):
    def boom(*a, **k):
        raise hermes_tools.requests.ConnectionError("down")
    monkeypatch.setattr(hermes_tools.requests, "get", boom)
    monkeypatch.setattr(hermes_tools, "cert_days_left", lambda d, t=10: 5.0)
    out = hermes_tools.run_tool("u-alice", "http_check", {"app": "shop"})
    assert "Không truy cập được" in out and "còn 5.0 ngày" in out


def test_redeploy_runs_in_background(env, monkeypatch):
    started = []
    monkeypatch.setattr(hermes_tools.cicd_core, "get_active_app_config", lambda app: {"branch": "dev"})
    monkeypatch.setattr(hermes_tools.cicd_core, "redeploy_app", lambda **kw: started.append(kw))
    out = hermes_tools.run_tool("u-alice", "redeploy_app", {"app": "shop"})
    time.sleep(0.05)
    assert "nhánh dev" in out
    assert started == [{"app_name": "shop", "branch": "dev", "pull_git": True, "target_component": None}]


def test_describe_rollback_and_set_env_hide_values():
    assert hermes_tools.describe_action("rollback_app", {"app": "shop", "revision": 2}) == "Rollback shop về revision 2"
    desc = hermes_tools.describe_action("apply_fix", {"app": "shop", "action": {
        "action_type": "set_env", "env_vars": {"JWT_SECRET": "super-secret"}, "description": "Bơm JWT"}})
    assert "JWT_SECRET=…" in desc and "super-secret" not in desc
