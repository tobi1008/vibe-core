"""
Hermes Monitor: tự động theo dõi hệ thống và báo về Telegram.

Mỗi MONITOR_INTERVAL giây, bộ giám sát kiểm tra:
  • Pod của từng ứng dụng: CrashLoopBackOff/không kéo được image (lỗi), chưa Ready, restart tăng.
  • Website của từng ứng dụng: HTTPS trả lỗi 5xx/không truy cập được; chứng chỉ sắp hết hạn.
  • Tài nguyên VPS: ổ đĩa, RAM, CPU của nút quản lý (đọc trực tiếp) và nút K3s (qua kubectl).
Cảnh báo chỉ gửi khi trạng thái chuyển từ tốt sang xấu (sau FAIL_THRESHOLD lần kiểm tra liên tiếp để tránh
báo nhầm lúc đang triển khai) và gửi tin "đã hồi phục" khi trở lại bình thường.
Mỗi ngày lúc DAILY_REPORT_HOUR giờ (GMT+7) gửi báo cáo tổng hợp.

Người nhận: cảnh báo của app gửi cho chủ app và admin đã liên kết Telegram; cảnh báo VPS chỉ gửi admin.
Toàn bộ là mã nguồn kiểm tra theo luật, không gọi LLM; người dùng bấm "Nhờ Hermes kiểm tra" nếu muốn chẩn đoán.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import builder_core
import hermes_tools
import hermes_verifier
import telegram_client as tg
import telegram_links
import user_directory

STATE_FILE = "/opt/vibe-hosting/monitor_state.json"
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "300"))
FAIL_THRESHOLD = 2
DISK_PCT = float(os.getenv("MONITOR_DISK_PCT", "85"))
MEM_PCT = float(os.getenv("MONITOR_MEM_PCT", "90"))
CPU_PCT = float(os.getenv("MONITOR_CPU_PCT", "90"))
CERT_WARN_DAYS = float(os.getenv("MONITOR_CERT_DAYS", "14"))
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "8"))
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DEPLOY_QUIET_SECONDS = 180
LOG_NAME = "hermes-monitor"

STATUS_ICON = {"ok": "🟢", "warn": "🟡", "bad": "🔴", "unknown": "⚪"}


# ─────────────────────────── Thu thập ───────────────────────────

def recently_deploying(app_name: str) -> bool:
    """Log build vừa được ghi trong DEPLOY_QUIET_SECONDS giây ⇒ đang triển khai, tạm không đánh giá"""
    path = os.path.join(builder_core.LOGS_DIR, f"{app_name}.log")
    try:
        return time.time() - os.path.getmtime(path) < DEPLOY_QUIET_SECONDS
    except OSError:
        return False


def evaluate_workload(info: Dict[str, Any]) -> Dict[str, Any]:
    """Phân loại một workload: bad (lỗi rõ ràng), warn (chưa Ready), ok"""
    if not info.get("found"):
        return {"level": "bad", "detail": f"{info['workload']} không còn trên K3s", "restarts": 0}
    restarts = sum(c.get("restarts", 0) for p in info.get("pods", []) for c in p.get("containers", []))
    reasons = hermes_verifier.blocking_reasons(info)
    ready, desired = info.get("ready_replicas", 0), info.get("replicas", 1)
    if reasons:
        return {"level": "bad", "detail": f"{info['workload']}: {', '.join(sorted(reasons))}", "restarts": restarts}
    if desired and ready < desired:
        return {"level": "warn", "detail": f"{info['workload']}: {ready}/{desired} Pod Ready", "restarts": restarts}
    return {"level": "ok", "detail": f"{info['workload']}: {ready}/{desired} Ready", "restarts": restarts}


def check_app(app_name: str) -> Dict[str, Any]:
    """Kết quả kiểm tra một app: các 'check' theo khóa, mỗi check có level và detail"""
    checks: Dict[str, Dict[str, Any]] = {}
    for workload in hermes_verifier.discover_workloads(app_name):
        checks[f"pod:{workload}"] = evaluate_workload(hermes_verifier.collect_workload(workload, with_logs=False))
    for domain in hermes_tools.app_domains(app_name):
        probe = hermes_tools.probe_domain(domain)
        if not probe["ok"]:
            detail = f"https://{domain}: " + (f"HTTP {probe['status_code']}" if "status_code" in probe else probe.get("error", "lỗi"))
            checks[f"http:{domain}"] = {"level": "bad", "detail": detail}
        else:
            checks[f"http:{domain}"] = {"level": "ok", "detail": f"https://{domain}: HTTP {probe['status_code']}, {probe['response_ms']} ms"}
        days = probe.get("cert_days_left")
        if days is not None:
            level = "warn" if days < CERT_WARN_DAYS else "ok"
            checks[f"cert:{domain}"] = {"level": level, "detail": f"Chứng chỉ {domain} còn {days:.0f} ngày"}
    return checks


def _read_meminfo() -> Optional[float]:
    try:
        values = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                values[key] = float(rest.split()[0])
        return 100 * (1 - values["MemAvailable"] / values["MemTotal"])
    except Exception:
        return None


def check_manager_node() -> Dict[str, Dict[str, Any]]:
    checks: Dict[str, Dict[str, Any]] = {}
    disk = shutil.disk_usage("/")
    pct = 100 * disk.used / disk.total
    checks["vps:manager:disk"] = {"level": "bad" if pct >= DISK_PCT else "ok",
                                  "detail": f"Nút quản lý – ổ đĩa {pct:.0f}% ({disk.free / 1e9:.1f} GB trống)"}
    mem = _read_meminfo()
    if mem is not None:
        checks["vps:manager:mem"] = {"level": "bad" if mem >= MEM_PCT else "ok", "detail": f"Nút quản lý – RAM {mem:.0f}%"}
    try:
        load = os.getloadavg()[1] / (os.cpu_count() or 1) * 100
        checks["vps:manager:cpu"] = {"level": "bad" if load >= CPU_PCT else "ok",
                                     "detail": f"Nút quản lý – tải CPU trung bình 5 phút {load:.0f}%"}
    except OSError:
        pass
    return checks


def _kubectl(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=60)


def check_k3s_nodes() -> Dict[str, Dict[str, Any]]:
    checks: Dict[str, Dict[str, Any]] = {}
    top = _kubectl(["top", "nodes", "--no-headers"])
    for line in top.stdout.splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        node, cpu_pct, mem_pct = cols[0], cols[2].rstrip("%"), cols[4].rstrip("%")
        if cpu_pct.isdigit():
            checks[f"vps:{node}:cpu"] = {"level": "bad" if float(cpu_pct) >= CPU_PCT else "ok", "detail": f"Nút {node} – CPU {cpu_pct}%"}
        if mem_pct.isdigit():
            checks[f"vps:{node}:mem"] = {"level": "bad" if float(mem_pct) >= MEM_PCT else "ok", "detail": f"Nút {node} – RAM {mem_pct}%"}
        stats = _kubectl(["get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary"])
        try:
            fs = json.loads(stats.stdout)["node"]["fs"]
            pct = 100 * fs["usedBytes"] / fs["capacityBytes"]
            checks[f"vps:{node}:disk"] = {"level": "bad" if pct >= DISK_PCT else "ok",
                                          "detail": f"Nút {node} – ổ đĩa {pct:.0f}% ({fs['availableBytes'] / 1e9:.1f} GB trống)"}
        except Exception:
            pass
    if top.returncode != 0 and not checks:
        checks["vps:k3s:api"] = {"level": "bad", "detail": f"Không lấy được số liệu nút K3s: {top.stderr.strip()[:150]}"}
    return checks


# ─────────────────────────── Bộ giám sát ───────────────────────────

class Monitor:
    def __init__(self) -> None:
        self.state = self._load_state()
        self.latest: Dict[str, Dict[str, Dict[str, Any]]] = {}  # app/"vps" -> checks lần gần nhất

    # ---- lưu trạng thái ----
    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data.setdefault("checks", {})
        data.setdefault("last_daily", "")
        return data

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            tmp = f"{STATE_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            builder_core.write_log(LOG_NAME, f"⚠️ Không lưu được trạng thái giám sát: {e}")

    # ---- đánh giá chuyển trạng thái ----
    def update_check(self, scope: str, key: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Cập nhật một check, trả về danh sách sự kiện cần báo (alert / recovered / restart)"""
        events: List[Dict[str, Any]] = []
        full_key = f"{scope}|{key}"
        prev = self.state["checks"].get(full_key, {"fails": 0, "alerted": False, "restarts": None})
        level = result["level"]

        if level == "ok":
            if prev.get("alerted"):
                events.append({"type": "recovered", "scope": scope, "detail": result["detail"]})
            prev.update({"fails": 0, "alerted": False})
        else:
            prev["fails"] = prev.get("fails", 0) + 1
            if prev["fails"] >= FAIL_THRESHOLD and not prev.get("alerted"):
                events.append({"type": "alert", "scope": scope, "level": level, "detail": result["detail"]})
                prev["alerted"] = True

        restarts = result.get("restarts")
        if restarts is not None:
            old = prev.get("restarts")
            if old is not None and restarts > old and level == "ok":
                events.append({"type": "restart", "scope": scope,
                               "detail": f"{result['detail']} – container vừa restart {restarts - old} lần"})
            prev["restarts"] = restarts

        self.state["checks"][full_key] = prev
        return events

    def forget_missing(self, scopes: List[str]) -> None:
        """Bỏ trạng thái của app đã bị xóa"""
        keep = set(scopes) | {"vps"}
        self.state["checks"] = {k: v for k, v in self.state["checks"].items() if k.split("|", 1)[0] in keep}

    def check_once(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        apps = hermes_tools.all_app_names()
        for app in apps:
            if recently_deploying(app):
                continue
            try:
                checks = check_app(app)
            except Exception as e:
                builder_core.write_log(LOG_NAME, f"⚠️ Kiểm tra {app} lỗi: {e}")
                continue
            self.latest[app] = checks
            for key, result in checks.items():
                events.extend(self.update_check(app, key, result))

        vps_checks: Dict[str, Dict[str, Any]] = {}
        for collector in (check_manager_node, check_k3s_nodes):
            try:
                vps_checks.update(collector())
            except Exception as e:
                builder_core.write_log(LOG_NAME, f"⚠️ {collector.__name__} lỗi: {e}")
        self.latest["vps"] = vps_checks
        for key, result in vps_checks.items():
            events.extend(self.update_check("vps", key, result))

        self.forget_missing(apps)
        self.latest = {k: v for k, v in self.latest.items() if k in apps or k == "vps"}
        self._save_state()
        return events

    # ---- người nhận ----
    @staticmethod
    def recipients(scope: str) -> List[int]:
        try:
            admins = user_directory.admin_ids()
            users = set(admins) if scope == "vps" else set(admins) | set(user_directory.owners_of(scope))
        except user_directory.DirectoryUnavailable:
            return []
        chats: List[int] = []
        for uid in sorted(users):
            chats.extend(telegram_links.chats_for_user(uid))
        return sorted(set(chats))

    @staticmethod
    def format_event(event: Dict[str, Any]) -> str:
        where = "VPS" if event["scope"] == "vps" else f"ứng dụng {event['scope']}"
        if event["type"] == "recovered":
            return f"✅ [{where}] Đã hoạt động bình thường trở lại.\n{event['detail']}"
        if event["type"] == "restart":
            return f"🟡 [{where}] {event['detail']}"
        icon = STATUS_ICON.get(event.get("level", "bad"), "🔴")
        return f"{icon} [{where}] Phát hiện sự cố:\n{event['detail']}"

    def notify(self, events: List[Dict[str, Any]]) -> None:
        for event in events:
            text = self.format_event(event)
            markup = None
            if event["scope"] != "vps" and event["type"] in ("alert", "restart"):
                markup = tg.inline_keyboard([[{"text": "🔍 Nhờ Hermes kiểm tra", "callback_data": f"dg:{event['scope']}"}]])
            for chat_id in self.recipients(event["scope"]):
                try:
                    tg.send_message(chat_id, text, markup)
                except Exception as e:
                    builder_core.write_log(LOG_NAME, f"⚠️ Gửi cảnh báo tới {chat_id} lỗi: {e}")

    # ---- báo cáo hằng ngày ----
    def app_summary(self, app: str) -> str:
        checks = self.latest.get(app)
        if checks is None:
            return f"⚪ {app}: chưa có số liệu (có thể đang triển khai)"
        levels = [c["level"] for c in checks.values()]
        worst = "bad" if "bad" in levels else "warn" if "warn" in levels else "ok"
        problems = [c["detail"] for c in checks.values() if c["level"] != "ok"]
        if not checks:
            return f"⚪ {app}: không có workload nào trên K3s"
        return f"{STATUS_ICON[worst]} {app}" + (": " + "; ".join(problems) if problems else ": hoạt động bình thường")

    def daily_report_for(self, user_id: str) -> str:
        now = datetime.now(TZ)
        apps = hermes_tools.visible_apps(user_id)
        lines = [f"📋 Báo cáo hằng ngày {now:%d/%m/%Y} – {len(apps)} ứng dụng"]
        lines.extend(self.app_summary(app) for app in apps)
        if user_directory.is_admin(user_id):
            vps = self.latest.get("vps", {})
            lines.append("\nTài nguyên VPS:")
            lines.extend(f"{STATUS_ICON[c['level']]} {c['detail']}" for c in vps.values())
        return "\n".join(lines)

    def daily_report_due(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(TZ)
        return now.hour >= DAILY_REPORT_HOUR and self.state.get("last_daily") != now.strftime("%Y-%m-%d")

    def send_daily_reports(self) -> None:
        self.state["last_daily"] = datetime.now(TZ).strftime("%Y-%m-%d")
        self._save_state()
        for chat_id, link in telegram_links.all_links().items():
            try:
                tg.send_message(chat_id, self.daily_report_for(link["user_id"]))
            except Exception as e:
                builder_core.write_log(LOG_NAME, f"⚠️ Gửi báo cáo ngày tới {chat_id} lỗi: {e}")

    # ---- vòng lặp ----
    def run_forever(self, stop: threading.Event) -> None:
        builder_core.write_log(LOG_NAME, f"🩺 Hermes Monitor chạy mỗi {MONITOR_INTERVAL} giây.")
        while not stop.is_set():
            try:
                events = self.check_once()
                if telegram_links.all_links():
                    self.notify(events)
                    if self.daily_report_due():
                        self.send_daily_reports()
            except Exception as e:
                builder_core.write_log(LOG_NAME, f"⚠️ Vòng giám sát lỗi: {e}")
            stop.wait(MONITOR_INTERVAL)


monitor = Monitor()
