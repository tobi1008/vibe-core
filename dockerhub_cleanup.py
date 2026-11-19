"""
Dọn rác Docker Hub do Vibe Hosting tạo ra, theo 2 bước: xem trước (plan) rồi mới xóa (execute).

- Repo mồ côi: không workload nào trên K3s dùng image của nó và không thuộc app nào còn trên panel
  (thường do app bị xóa khi chưa xóa được repo) => xóa cả repo.
- Tag cũ: mỗi lần build tạo một tag theo thời điểm => trong repo còn dùng, giữ các tag đang được
  K3s tham chiếu (kể cả ReplicaSet cũ để rollback) + DOCKERHUB_KEEP_TAGS tag mới nhất, xóa phần còn lại.

An toàn:
- Repo trong DOCKERHUB_PROTECTED_REPOS (cách nhau bởi dấu phẩy) không bao giờ bị đụng tới.
- Repo / tag vừa cập nhật trong DOCKERHUB_GRACE_HOURS giờ bị bỏ qua (pipeline có thể đang push mà chưa apply).
- Không đọc được image đang dùng từ K3s => không xóa gì.
- Execute chỉ xóa những mục vừa được chọn VÀ vẫn nằm trong kế hoạch tính lại ngay lúc đó.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import requests

import builder_core
from builder_core import DOCKER_HUB_API, DOCKER_USER, dockerhub_token

KEEP_TAGS = int(os.getenv("DOCKERHUB_KEEP_TAGS", "3"))
GRACE_HOURS = float(os.getenv("DOCKERHUB_GRACE_HOURS", "1"))
LOG_NAME = "dockerhub-cleanup"


class CleanupError(Exception):
    pass


def protected_repos() -> Set[str]:
    raw = os.getenv("DOCKERHUB_PROTECTED_REPOS", "")
    return {r.strip().split("/")[-1] for r in raw.split(",") if r.strip()}


def _get_all(url: str, token: str) -> List[Dict[str, Any]]:
    """Đọc hết các trang của một API danh sách Docker Hub"""
    items = []
    while url:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if resp.status_code != 200:
            raise CleanupError(f"Docker Hub trả về HTTP {resp.status_code} khi đọc {url}")
        data = resp.json()
        items += data.get("results", [])
        url = data.get("next")
    return items


def list_repos(token: str) -> List[Dict[str, Any]]:
    return _get_all(f"{DOCKER_HUB_API}/repositories/{DOCKER_USER}/?page_size=100", token)


def list_tags(token: str, repo: str) -> List[Dict[str, Any]]:
    return _get_all(f"{DOCKER_HUB_API}/repositories/{DOCKER_USER}/{repo}/tags/?page_size=100", token)


def images_in_use() -> Set[str]:
    """Mọi image (repo:tag) mà K3s đang tham chiếu ở mọi namespace, gồm cả ReplicaSet cũ dùng để rollback"""
    res = subprocess.run(
        ["kubectl", "get", "deployments,statefulsets,daemonsets,replicasets,cronjobs,jobs,pods", "-A", "-o", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise CleanupError(f"Không đọc được workload từ K3s: {res.stderr.strip()[:200]}")
    images = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "image" and isinstance(v, str):
                    images.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(res.stdout).get("items", []))
    return images


def used_tags_by_repo(images: Set[str]) -> Dict[str, Set[str]]:
    """{"shop": {"1795100798"}} cho các image thuộc tài khoản DOCKER_USER"""
    used: Dict[str, Set[str]] = {}
    for image in images:
        ref = image.split("@")[0]
        for prefix in (f"docker.io/{DOCKER_USER}/", f"{DOCKER_USER}/"):
            if ref.startswith(prefix):
                name, _, tag = ref[len(prefix):].partition(":")
                used.setdefault(name, set()).add(tag or "latest")
    return used


def panel_apps() -> Set[str]:
    """App còn trên panel (workspace hoặc cấu hình) => không coi repo của nó là mồ côi dù chưa chạy trên K3s"""
    apps = set()
    try:
        apps |= set(os.listdir(builder_core.BASE_WORKSPACE))
    except Exception:
        pass
    try:
        import main
        with open(main.CONFIG_DB_FILE, "r", encoding="utf-8") as f:
            apps |= set(json.load(f).keys())
    except Exception:
        pass
    for name in list(apps):
        for suffix in ("-be", "-fe", "-monorepo"):
            if name.endswith(suffix):
                apps.add(name[: -len(suffix)])
    return {r for a in apps for r in builder_core.image_repositories(a)}


def _recent(ts: Optional[str], now: datetime) -> bool:
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - when < timedelta(hours=GRACE_HOURS)


def build_plan(token: Optional[str] = None) -> Dict[str, Any]:
    """
    Kế hoạch dọn dẹp, không xóa gì:
    {"orphan_repos": [{"repo", "tags", "last_updated"}], "old_tags": {"repo": ["tag", ...]}, "kept": {...}, "protected": [...]}
    """
    token = token or dockerhub_token()
    if not token:
        raise CleanupError("Đăng nhập Docker Hub thất bại: chưa có DOCKER_PASSWORD và không đọc được thông tin docker login trên máy chủ, hoặc thông tin đó sai.")

    now = datetime.now(timezone.utc)
    used = used_tags_by_repo(images_in_use())
    protected = protected_repos()
    on_panel = panel_apps()

    plan: Dict[str, Any] = {"orphan_repos": [], "old_tags": {}, "skipped": [], "protected": sorted(protected),
                            "keep_tags": KEEP_TAGS, "grace_hours": GRACE_HOURS}
    for repo in list_repos(token):
        name = repo["name"]
        if name in protected:
            continue
        if _recent(repo.get("last_updated"), now):
            plan["skipped"].append({"repo": name, "reason": f"vừa cập nhật trong {GRACE_HOURS:g} giờ qua"})
            continue
        tags = list_tags(token, name)

        if name not in used and name not in on_panel:
            plan["orphan_repos"].append({"repo": name, "tags": len(tags), "last_updated": repo.get("last_updated")})
            continue

        in_use = used.get(name, set())
        newest = sorted(tags, key=lambda t: t.get("last_updated") or "", reverse=True)
        keep = {t["name"] for t in newest[:KEEP_TAGS]} | in_use
        old = [t["name"] for t in newest if t["name"] not in keep and not _recent(t.get("last_updated"), now)]
        if old:
            plan["old_tags"][name] = old
    return plan


def _delete(url: str, token: str) -> str:
    try:
        resp = requests.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except Exception as e:
        return f"error: {e}"
    if resp.status_code in (200, 202, 204):
        return "deleted"
    if resp.status_code == 404:
        return "not_found"
    if resp.status_code in (401, 403):
        return f"error: HTTP {resp.status_code} (token không có quyền Delete)"
    return f"error: HTTP {resp.status_code}"


def execute(repos: List[str], tags: Dict[str, List[str]]) -> Dict[str, Any]:
    """Xóa các mục người dùng đã chọn, chỉ khi chúng vẫn nằm trong kế hoạch tính lại ngay lúc này"""
    token = dockerhub_token()
    if not token:
        raise CleanupError("Đăng nhập Docker Hub thất bại: kiểm tra DOCKER_PASSWORD hoặc docker login trên máy chủ.")
    plan = build_plan(token)
    allowed_repos = {r["repo"] for r in plan["orphan_repos"]}
    result: Dict[str, Any] = {"repos": {}, "tags": {}, "refused": []}

    for repo in repos:
        if repo not in allowed_repos:
            result["refused"].append(repo)
            continue
        status = _delete(f"{DOCKER_HUB_API}/repositories/{DOCKER_USER}/{repo}/", token)
        result["repos"][repo] = status
        builder_core.write_log(LOG_NAME, f"[DOCKER HUB] Xóa repository {DOCKER_USER}/{repo}: {status}")

    for repo, names in (tags or {}).items():
        allowed = set(plan["old_tags"].get(repo, []))
        for tag in names:
            if tag not in allowed:
                result["refused"].append(f"{repo}:{tag}")
                continue
            status = _delete(f"{DOCKER_HUB_API}/repositories/{DOCKER_USER}/{repo}/tags/{tag}/", token)
            result["tags"].setdefault(repo, {})[tag] = status
            builder_core.write_log(LOG_NAME, f"[DOCKER HUB] Xóa tag {DOCKER_USER}/{repo}:{tag}: {status}")
    return result
