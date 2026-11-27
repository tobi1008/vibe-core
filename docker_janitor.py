"""
Dọn dung lượng Docker trên máy build sau mỗi lần triển khai.

Mỗi lần build, Docker giữ lại build cache và image mới. Image đã đẩy lên Docker Hub nên bản trên máy
build không còn cần, nếu không dọn thì ổ đĩa đầy dần (đã từng lên 22 GB cache + 8 GB image).

Sau mỗi pipeline (thành công hay thất bại), chạy nền:
  • docker builder prune: giữ build cache tối đa DOCKER_BUILD_CACHE_MAX (mặc định 5gb) để build sau vẫn nhanh.
  • docker image prune -a: xóa image không có container nào dùng và đã tạo quá DOCKER_IMAGE_MAX_AGE
    (mặc định 2h). Ngưỡng thời gian để không xóa nhầm image của một pipeline khác đang build dở chưa push.
Đặt DOCKER_JANITOR=0 để tắt.
"""

import os
import subprocess
import threading
from typing import Callable, List, Optional

CACHE_MAX = os.getenv("DOCKER_BUILD_CACHE_MAX", "5gb")
IMAGE_MAX_AGE = os.getenv("DOCKER_IMAGE_MAX_AGE", "2h")
TIMEOUT = 600

_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("DOCKER_JANITOR", "1") != "0"


def commands() -> List[List[str]]:
    return [
        ["docker", "builder", "prune", "-f", "--max-used-space", CACHE_MAX],
        ["docker", "image", "prune", "-af", "--filter", f"until={IMAGE_MAX_AGE}"],
    ]


def _reclaimed(output: str) -> Optional[str]:
    for line in reversed((output or "").splitlines()):
        if "reclaimed" in line.lower() or line.strip().lower().startswith("total:"):
            return line.split(":", 1)[-1].strip()
    return None


def cleanup(app_name: str, log: Callable[[str, str], None]) -> None:
    """Chạy tuần tự các lệnh dọn; nếu một lần dọn khác đang chạy thì bỏ qua lượt này"""
    if not enabled() or not _lock.acquire(blocking=False):
        return
    try:
        freed = []
        for cmd in commands():
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
