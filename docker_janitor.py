"""
Dọn dung lượng Docker trên máy build sau mỗi lần triển khai.

Mỗi lần build, Docker giữ lại build cache và image mới. Image đã đẩy lên Docker Hub nên bản trên máy
build không còn cần, nếu không dọn thì ổ đĩa đầy dần (đã từng lên 22 GB cache + 8 GB image).

Sau mỗi pipeline (thành công hay thất bại), chạy nền:
  • docker builder prune: giữ build cache tối đa DOCKER_BUILD_CACHE_MAX (mặc định 5gb) để build sau vẫn nhanh.
  • docker image prune -a: xóa image không có container nào dùng và đã tạo quá DOCKER_IMAGE_MAX_AGE
    (mặc định 2h). Ngưỡng thời gian để không xóa nhầm image của một pipeline khác đang build dở chưa push.
Ngoài ra luồng nền run_forever (chạy cùng API) là lưới an toàn khi lâu không có ai build hoặc build lỗi giữa chừng:
  • Mỗi DOCKER_JANITOR_INTERVAL giờ (mặc định 6) dọn như trên.
  • Mỗi 5 phút đo ổ đĩa; vượt DOCKER_JANITOR_DISK_PCT % (mặc định 80) thì dọn mạnh: chỉ giữ
    DOCKER_BUILD_CACHE_MIN (mặc định 1gb) build cache.
Đặt DOCKER_JANITOR=0 để tắt.
"""

import os
import shutil
import subprocess
import threading
import time
from typing import Callable, List, Optional

CACHE_MAX = os.getenv("DOCKER_BUILD_CACHE_MAX", "5gb")
IMAGE_MAX_AGE = os.getenv("DOCKER_IMAGE_MAX_AGE", "2h")
CACHE_MIN = os.getenv("DOCKER_BUILD_CACHE_MIN", "1gb")
INTERVAL_SECONDS = float(os.getenv("DOCKER_JANITOR_INTERVAL", "6")) * 3600
DISK_PCT = float(os.getenv("DOCKER_JANITOR_DISK_PCT", "80"))
DISK_CHECK_SECONDS = 300
AGGRESSIVE_COOLDOWN_SECONDS = 3600  # ổ đầy vì thứ khác ngoài Docker thì không dọn/ghi log mỗi 5 phút
TIMEOUT = 600
LOG_NAME = "docker-janitor"

_lock = threading.Lock()
_last_aggressive = 0.0


def enabled() -> bool:
    return os.getenv("DOCKER_JANITOR", "1") != "0"


def commands(aggressive: bool = False) -> List[List[str]]:
    return [
        ["docker", "builder", "prune", "-f", "--max-used-space", CACHE_MIN if aggressive else CACHE_MAX],
        ["docker", "image", "prune", "-af", "--filter", f"until={IMAGE_MAX_AGE}"],
    ]


def _reclaimed(output: str) -> Optional[str]:
    for line in reversed((output or "").splitlines()):
        if "reclaimed" in line.lower() or line.strip().lower().startswith("total:"):
            return line.split(":", 1)[-1].strip()
    return None


def cleanup(app_name: str, log: Callable[[str, str], None], aggressive: bool = False) -> None:
    """Chạy tuần tự các lệnh dọn; nếu một lần dọn khác đang chạy thì bỏ qua lượt này"""
    if not enabled() or not _lock.acquire(blocking=False):
        return
    try:
        freed = []
        for cmd in commands(aggressive):
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
            except Exception as e:
                log(app_name, f"⚠️ [DỌN DOCKER] {' '.join(cmd[:3])} lỗi: {e}")
                continue
            if res.returncode != 0:
                log(app_name, f"⚠️ [DỌN DOCKER] {' '.join(cmd[:3])} lỗi: {(res.stderr or res.stdout).strip()[:200]}")
                continue
            amount = _reclaimed(res.stdout)
            if amount and not amount.startswith("0B"):
                freed.append(f"{cmd[1]} {amount}")
        if freed:
            log(app_name, f"🧹 [DỌN DOCKER] Đã giải phóng: {', '.join(freed)}")
    finally:
        _lock.release()


def cleanup_async(app_name: str, log: Callable[[str, str], None]) -> None:
    if enabled():
        threading.Thread(target=cleanup, args=(app_name, log), daemon=True, name="docker-janitor").start()


def disk_percent(path: str = "/") -> float:
    usage = shutil.disk_usage(path)
    return 100 * usage.used / usage.total


def tick(now: float, last_periodic: float, log: Callable[[str, str], None]) -> float:
    """Một lượt kiểm tra của luồng nền; trả về thời điểm dọn định kỳ gần nhất"""
    try:
        pct = disk_percent()
    except OSError:
        pct = 0.0
    global _last_aggressive
    if pct >= DISK_PCT and now - _last_aggressive >= AGGRESSIVE_COOLDOWN_SECONDS:
        _last_aggressive = now
        log(LOG_NAME, f"⚠️ Ổ đĩa {pct:.0f}% (ngưỡng {DISK_PCT:.0f}%), dọn mạnh build cache và image cũ.")
        cleanup("hệ thống", lambda app, msg: log(LOG_NAME, msg), aggressive=True)
        return now
    if now - last_periodic >= INTERVAL_SECONDS:
        cleanup("định kỳ", lambda app, msg: log(LOG_NAME, f"[{app}] {msg}"))
        return now
    return last_periodic


def run_forever(stop: threading.Event, log: Callable[[str, str], None]) -> None:
    last_periodic = 0.0  # lượt đầu dọn luôn khi API khởi động
    while not stop.is_set():
        if enabled():
            try:
                last_periodic = tick(time.time(), last_periodic, log)
            except Exception as e:
                log(LOG_NAME, f"⚠️ Lỗi luồng dọn Docker: {e}")
        stop.wait(DISK_CHECK_SECONDS)


def start_background(stop: threading.Event, log: Callable[[str, str], None]) -> bool:
    if not enabled():
        return False
    threading.Thread(target=run_forever, args=(stop, log), daemon=True, name="docker-janitor-loop").start()
    return True
