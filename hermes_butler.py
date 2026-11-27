"""
Hermes Butler: quản gia hệ thống Vibe Hosting, trò chuyện với người dùng qua Telegram.

Vòng lặp tác tử (agent loop):
  1. Vibe Core gửi cho Hermes LLM: vai trò, danh sách công cụ, danh sách app của người dùng,
     lịch sử hội thoại và kết quả các công cụ đã chạy ở lượt này.
  2. Hermes trả về đúng MỘT đối tượng JSON: {"tool": ..., "args": {...}} hoặc {"reply": "..."}.
  3. Công cụ đọc được Vibe Core chạy ngay rồi quay lại bước 1 (tối đa MAX_STEPS bước).
     Công cụ thay đổi hệ thống KHÔNG được chạy: Vibe Core tạo yêu cầu duyệt và gửi nút cho người dùng.
  4. Khi người dùng bấm "Duyệt", Vibe Core kiểm tra lại quyền rồi mới thực thi.

Hermes LLM chỉ chọn công cụ và viết câu trả lời; việc kiểm tra quyền, thực thi lệnh và quyết định
có được thực thi hay không do mã nguồn Vibe Core đảm nhiệm.
"""

import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import builder_core
import hermes_tools
import telegram_client as tg
import telegram_links
import user_directory

MAX_STEPS = 6
HISTORY_TURNS = 10
APPROVAL_TTL = 30 * 60
LLM_TIMEOUT = 60
LOG_NAME = "hermes-butler"
WEB_URL = "https://vibe.quyenlt.com"

SYSTEM_PROMPT = """Bạn là Hermes, quản gia hệ thống của nền tảng Vibe Hosting (triển khai ứng dụng web lên cụm K3s).
Bạn trò chuyện với người dùng qua Telegram bằng tiếng Việt, ngắn gọn, dễ hiểu với người không chuyên DevOps.

Bạn có các công cụ sau (do hệ thống thực thi):
{tools}

QUY TẮC:
1. Mỗi lượt chỉ trả về MỘT đối tượng JSON, không kèm chữ nào khác, không dùng ```:
   - Gọi công cụ: {{"tool": "tên_công_cụ", "args": {{...}}}}
   - Trả lời người dùng: {{"reply": "nội dung trả lời"}}
2. Không đoán trạng thái hệ thống. Muốn biết thì gọi công cụ đọc (app_status, runtime_logs, k8s_events, http_check...).
3. Chỉ thao tác trên các ứng dụng trong danh sách của người dùng.
4. Các công cụ restart_app, rollback_app, redeploy_app thay đổi hệ thống: hệ thống sẽ gửi nút để người dùng duyệt,
   bạn không cần hỏi lại bằng lời. Ưu tiên cách ít rủi ro: xem log và trạng thái trước, rồi mới đề xuất khởi động lại,
   rollback hoặc build lại. Khi cần sửa cấu hình/mã nguồn, dùng công cụ diagnose.
5. Câu trả lời cuối nêu rõ: tình trạng hiện tại, nguyên nhân (nếu tìm được) và việc nên làm tiếp theo.

Ứng dụng của người dùng: {apps}
"""


# ─────────────────────────── Yêu cầu duyệt ───────────────────────────

_approvals: Dict[str, Dict[str, Any]] = {}
_approvals_lock = threading.Lock()


def create_approval(chat_id: int, user_id: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    approval = {
        "id": secrets.token_hex(6),
        "chat_id": chat_id,
        "user_id": user_id,
        "tool": tool,
        "args": args,
        "description": hermes_tools.describe_action(tool, args),
        "created_at": time.time(),
    }
    with _approvals_lock:
        now = time.time()
        for key in [k for k, v in _approvals.items() if now - v["created_at"] > APPROVAL_TTL]:
            del _approvals[key]
        _approvals[approval["id"]] = approval
    return approval


def take_approval(approval_id: str, chat_id: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """Lấy và xóa yêu cầu duyệt; chỉ đúng chat đã nhận yêu cầu mới lấy được"""
    with _approvals_lock:
        approval = _approvals.get(approval_id)
        if not approval:
            return None, "Yêu cầu này không còn hiệu lực (đã xử lý hoặc quá 30 phút)."
        if approval["chat_id"] != chat_id:
            return None, "Bạn không phải người nhận yêu cầu này."
        if time.time() - approval["created_at"] > APPROVAL_TTL:
            del _approvals[approval_id]
            return None, "Yêu cầu đã quá 30 phút, hãy hỏi lại Hermes."
        del _approvals[approval_id]
    return approval, ""


def approval_keyboard(approval: Dict[str, Any]) -> Dict[str, Any]:
    return tg.inline_keyboard([[
        {"text": "✅ Duyệt", "callback_data": f"ap:{approval['id']}"},
        {"text": "✖️ Bỏ qua", "callback_data": f"rj:{approval['id']}"},
    ]])


# ─────────────────────────── Vòng lặp tác tử ───────────────────────────

def call_llm(prompt: str) -> str:
    return builder_core.run_hermes_prompt(prompt, app_name=LOG_NAME, timeout=LLM_TIMEOUT, max_models=3)


def parse_decision(raw: str) -> Dict[str, Any]:
    """Tách JSON quyết định từ câu trả lời của LLM; không phải JSON thì coi như câu trả lời thường"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and (data.get("tool") or "reply" in data):
                if data.get("tool") and not isinstance(data.get("args"), dict):
                    data["args"] = {}
                return data
        except json.JSONDecodeError:
            pass
    return {"reply": text or "Hermes chưa đưa ra được câu trả lời."}


def build_prompt(user_id: str, history: List[Dict[str, str]], message: str, steps: List[str]) -> str:
    try:
        apps = ", ".join(hermes_tools.visible_apps(user_id)) or "(chưa có ứng dụng)"
    except user_directory.DirectoryUnavailable:
        apps = "(không tra cứu được)"
    parts = [SYSTEM_PROMPT.format(tools=hermes_tools.describe_tools(), apps=apps)]
    if history:
        parts.append("--- HỘI THOẠI TRƯỚC ---")
        for h in history[-HISTORY_TURNS:]:
            speaker = "Người dùng" if h["role"] == "user" else "Hermes"
            parts.append(f"{speaker}: {h['content']}")
    parts.append(f"--- TIN NHẮN MỚI ---\nNgười dùng: {message}")
    if steps:
        parts.append("--- CÁC BƯỚC ĐÃ THỰC HIỆN Ở LƯỢT NÀY ---")
        parts.extend(steps)
    parts.append("Trả về JSON cho bước tiếp theo:")
    return "\n\n".join(parts)


def format_diagnosis(result: Dict[str, Any]) -> str:
    lines = [f"🩺 Chẩn đoán {result.get('app', '')}:"]
    if result.get("diagnosis"):
        lines.append(result["diagnosis"])
    if result.get("root_cause"):
        lines.append(f"Nguyên nhân gốc: {result['root_cause']}")
    if result.get("suggested_solution"):
        lines.append(f"Hướng xử lý: {result['suggested_solution']}")
    return "\n".join(lines)


def run_agent(chat_id: int, user_id: str, history: List[Dict[str, str]], message: str,
              llm: Callable[[str], str] = None) -> Dict[str, Any]:
    """Chạy một lượt hội thoại. Trả về {"reply": str, "approvals": [...]}; không gửi gì ra Telegram"""
    llm = llm or call_llm
    steps: List[str] = []
    approvals: List[Dict[str, Any]] = []
    for _ in range(MAX_STEPS):
        try:
            decision = parse_decision(llm(build_prompt(user_id, history, message, steps)))
        except Exception as e:
            builder_core.write_log(LOG_NAME, f"⚠️ Hermes LLM lỗi: {e}")
            return {"reply": "⚠️ Hermes LLM đang không phản hồi (đã thử 3 model). Bạn thử lại sau ít phút nhé.",
                    "approvals": approvals}

        tool_name = decision.get("tool")
        if not tool_name:
            return {"reply": str(decision.get("reply", "")).strip() or "Hermes chưa có câu trả lời.", "approvals": approvals}

        args = decision.get("args") or {}
        tool = hermes_tools.get_tool(tool_name)
        if not tool:
            steps.append(f"[{tool_name}] LỖI: không có công cụ này.")
            continue

        try:
            if tool_name == "diagnose":
                result = hermes_tools.diagnose(user_id, args)
                for action in result.get("actions") or []:
                    approvals.append(create_approval(chat_id, user_id, "apply_fix", {"app": result["app"], "action": action}))
                reply = format_diagnosis(result)
                reply += ("\n\nHermes đề xuất các bước khắc phục bên dưới, bạn bấm Duyệt cho bước muốn thực hiện."
                          if approvals else "\n\nHermes không đề xuất thao tác tự động nào.")
                return {"reply": reply, "approvals": approvals}

            if tool["mutating"]:
                hermes_tools.check_access(user_id, args.get("app"))
                approvals.append(create_approval(chat_id, user_id, tool_name, args))
                reason = str(decision.get("reason") or decision.get("reply") or "").strip()
                reply = (reason + "\n\n" if reason else "") + "Hermes cần bạn duyệt thao tác bên dưới."
                return {"reply": reply, "approvals": approvals}

            output = hermes_tools.run_tool(user_id, tool_name, args)
            steps.append(f"[{tool_name} {json.dumps(args, ensure_ascii=False)}] KẾT QUẢ:\n{output}")
        except hermes_tools.ToolError as e:
            steps.append(f"[{tool_name}] LỖI: {e}")
        except user_directory.DirectoryUnavailable:
            return {"reply": "⚠️ Không tra cứu được tài khoản trên Vibe Hosting, bạn thử lại sau nhé.", "approvals": approvals}
        except Exception as e:
            builder_core.write_log(LOG_NAME, f"⚠️ Công cụ {tool_name} lỗi: {e}")
            steps.append(f"[{tool_name}] LỖI HỆ THỐNG: {str(e)[:200]}")

    summary = "\n\n".join(steps[-2:])
    return {"reply": "Hermes đã kiểm tra nhưng chưa đi đến kết luận. Kết quả gần nhất:\n\n" + summary,
            "approvals": approvals}


# ─────────────────────────── Bot Telegram ───────────────────────────

# Long polling ngắn để đổi token trên web có hiệu lực trong vòng nửa phút
POLL_TIMEOUT = 25
TOKEN_RECHECK_SECONDS = 5

HELP_TEXT = (
    "Mình là Hermes, quản gia của Vibe Hosting. Bạn có thể nhắn tự nhiên, ví dụ:\n"
    "• \"app shop đang lỗi gì vậy?\"\n"
    "• \"xem log backend của shop\"\n"
    "• \"rollback shop về bản trước\"\n\n"
    "Lệnh nhanh:\n"
    "/apps – danh sách ứng dụng\n"
    "/status <app> – trạng thái ứng dụng\n"
    "/reset – bắt đầu hội thoại mới\n"
    "/unlink – hủy liên kết Telegram\n\n"
    "Mọi thao tác thay đổi hệ thống đều cần bạn bấm Duyệt. Hermes cũng tự theo dõi và báo khi ứng dụng gặp sự cố."
)

NOT_LINKED_TEXT = (
    "Telegram này chưa liên kết với tài khoản Vibe Hosting.\n"
    f"Vào {WEB_URL}/profile, bấm \"Liên kết Telegram\" rồi mở đường dẫn bot gửi cho bạn."
)


class Butler:
    def __init__(self) -> None:
        self.history: Dict[int, List[Dict[str, str]]] = {}
        self.chat_locks: Dict[int, threading.Lock] = {}
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hermes-butler")
        self._locks_guard = threading.Lock()

    # ---- gửi tin ----
    def send(self, chat_id: int, text: str, approvals: Optional[List[Dict[str, Any]]] = None) -> None:
        try:
            tg.send_message(chat_id, text)
            for approval in approvals or []:
                tg.send_message(chat_id, f"🔧 Đề xuất: {approval['description']}", approval_keyboard(approval))
        except Exception as e:
            builder_core.write_log(LOG_NAME, f"⚠️ Gửi Telegram thất bại: {e}")

    def _chat_lock(self, chat_id: int) -> threading.Lock:
        with self._locks_guard:
            return self.chat_locks.setdefault(chat_id, threading.Lock())

    # ---- xử lý update ----
    def handle_update(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return  # chỉ phục vụ chat riêng, tránh lộ thông tin trong nhóm
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return
        sender = message.get("from", {})
        tg_name = sender.get("username") or sender.get("first_name") or ""

        if text.startswith("/start"):
            self.handle_start(chat_id, text, tg_name)
            return

        link = telegram_links.get_link(chat_id)
        if not link:
            self.send(chat_id, NOT_LINKED_TEXT)
            return
        user_id = link["user_id"]

        command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        if command == "/help":
            self.send(chat_id, HELP_TEXT)
        elif command == "/reset":
            self.history.pop(chat_id, None)
            self.send(chat_id, "Đã xóa lịch sử hội thoại.")
        elif command == "/unlink":
            telegram_links.unlink_chat(chat_id)
            self.history.pop(chat_id, None)
            self.send(chat_id, "Đã hủy liên kết. Hermes sẽ không gửi thông báo cho Telegram này nữa.")
        elif command == "/apps":
            self.executor.submit(self._direct_tool, chat_id, user_id, "list_apps", {})
        elif command == "/status":
            parts = text.split()
            if len(parts) < 2:
                self.send(chat_id, "Cú pháp: /status <tên app>")
            else:
                self.executor.submit(self._direct_tool, chat_id, user_id, "app_status", {"app": parts[1]})
        elif command:
            self.send(chat_id, "Lệnh không hỗ trợ. Gõ /help để xem hướng dẫn.")
        else:
            self.executor.submit(self.converse, chat_id, user_id, text)

    def handle_start(self, chat_id: int, text: str, tg_name: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            link = telegram_links.consume_link_code(parts[1], chat_id, tg_name)
            if not link:
                self.send(chat_id, "Mã liên kết không đúng hoặc đã hết hạn (10 phút). Hãy tạo mã mới trên trang hồ sơ.")
                return
            name = link.get("username") or "bạn"
            self.send(chat_id, f"✅ Đã liên kết Telegram với tài khoản {name}.\n\n{HELP_TEXT}")
            return
        if telegram_links.get_link(chat_id):
            self.send(chat_id, HELP_TEXT)
        else:
            self.send(chat_id, NOT_LINKED_TEXT)

    def _direct_tool(self, chat_id: int, user_id: str, tool: str, args: Dict[str, Any]) -> None:
        try:
            self.send(chat_id, hermes_tools.run_tool(user_id, tool, args))
        except hermes_tools.ToolError as e:
            self.send(chat_id, f"⚠️ {e}")
        except user_directory.DirectoryUnavailable:
            self.send(chat_id, "⚠️ Không tra cứu được tài khoản trên Vibe Hosting, bạn thử lại sau nhé.")
        except Exception as e:
            builder_core.write_log(LOG_NAME, f"⚠️ {tool} lỗi: {e}")
            self.send(chat_id, "⚠️ Có lỗi khi thực hiện, bạn thử lại sau nhé.")

    def converse(self, chat_id: int, user_id: str, text: str) -> None:
        with self._chat_lock(chat_id):
            tg.send_chat_action(chat_id)
            history = self.history.setdefault(chat_id, [])
            result = run_agent(chat_id, user_id, history, text)
            history.append({"role": "user", "content": text[:1000]})
            history.append({"role": "assistant", "content": result["reply"][:1000]})
            del history[:-HISTORY_TURNS * 2]
            self.send(chat_id, result["reply"], result["approvals"])

    def handle_callback(self, callback: Dict[str, Any]) -> None:
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        tg.answer_callback(callback.get("id", ""))
        if not chat_id:
            return
        link = telegram_links.get_link(chat_id)
        if not link:
            self.send(chat_id, NOT_LINKED_TEXT)
            return

        kind, _, value = data.partition(":")
        if kind in ("ap", "rj"):
            approval, error = take_approval(value, chat_id)
            if message_id:
                tg.clear_keyboard(chat_id, message_id)
            if not approval:
                self.send(chat_id, error)
                return
            if approval["user_id"] != link["user_id"]:
                self.send(chat_id, "Liên kết Telegram đã thay đổi, yêu cầu này bị hủy.")
                return
            if kind == "rj":
                self.send(chat_id, f"Đã bỏ qua: {approval['description']}")
                return
            self.executor.submit(self.execute_approval, approval)
        elif kind == "dg":
            text = f"Ứng dụng {value} vừa được báo có sự cố. Hãy kiểm tra nguyên nhân và đề xuất cách xử lý."
            self.executor.submit(self.converse, chat_id, link["user_id"], text)

    def execute_approval(self, approval: Dict[str, Any]) -> None:
        chat_id = approval["chat_id"]
        self.send(chat_id, f"⏳ Đang thực hiện: {approval['description']}")
        try:
            # Kiểm tra quyền lại tại thời điểm thực thi (quyền có thể đã thay đổi sau khi đề xuất)
            output = hermes_tools.run_tool(approval["user_id"], approval["tool"], approval["args"], include_internal=True)
            builder_core.write_log(LOG_NAME, f"✅ user {approval['user_id']} duyệt: {approval['description']}")
            self.send(chat_id, f"✅ {output}")
        except hermes_tools.ToolError as e:
            self.send(chat_id, f"⚠️ Không thực hiện được: {e}")
        except Exception as e:
            builder_core.write_log(LOG_NAME, f"⚠️ Thực thi {approval['tool']} lỗi: {e}")
            self.send(chat_id, f"⚠️ Lỗi khi thực hiện: {str(e)[:300]}")

    # ---- long polling ----
    def poll_forever(self, stop: threading.Event) -> None:
        """Chạy suốt vòng đời API. Chưa có token thì chờ; admin đổi token trên web thì tự chuyển sang bot mới"""
        offset: Optional[int] = None
        current_token = ""
        while not stop.is_set():
            token = tg.bot_token()
            if not token:
                if current_token:
                    builder_core.write_log(LOG_NAME, "⏸️ Bot Telegram đã bị gỡ, Hermes Butler tạm dừng.")
                current_token = ""
                tg.write_runtime_status(running=False)
                stop.wait(TOKEN_RECHECK_SECONDS)
                continue
            if token != current_token:
                builder_core.write_log(LOG_NAME, "🤖 Hermes Butler bắt đầu nhận tin nhắn Telegram.")
                current_token, offset = token, None
            try:
                updates = tg.get_updates(offset, poll_timeout=POLL_TIMEOUT)
                tg.write_runtime_status(running=True)
            except Exception as e:
                builder_core.write_log(LOG_NAME, f"⚠️ getUpdates lỗi: {e}")
                tg.write_runtime_status(running=True, error=str(e))
                stop.wait(10)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception as e:
                    builder_core.write_log(LOG_NAME, f"⚠️ Xử lý update lỗi: {e}")


butler = Butler()
