"""
Cấu hình chung cho bộ test:
- Đặt VIBE_API_KEY giả trước khi import ứng dụng
- Chặn việc tạo thư mục /opt/vibe-hosting/* lúc import (máy dev không có quyền ghi)
- Chuyển toàn bộ đường dẫn runtime sang thư mục tạm của từng test
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

API_KEY = "test-api-key"
os.environ["VIBE_API_KEY"] = API_KEY
os.environ["GITHUB_WEBHOOK_SECRET"] = ""

_real_makedirs = os.makedirs


def _skip_opt_makedirs(path, *args, **kwargs):
    if str(path).startswith("/opt/vibe-hosting"):
        return None
    return _real_makedirs(path, *args, **kwargs)


os.makedirs = _skip_opt_makedirs
try:
    import archive_builder_core  # noqa: E402
    import builder_core  # noqa: E402
    import cicd_core  # noqa: E402
    import file_manager_api  # noqa: E402
    import hermes_agent_router  # noqa: E402
    import hermes_verifier  # noqa: E402
    import main  # noqa: E402
finally:
    os.makedirs = _real_makedirs


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Trỏ mọi đường dẫn /opt/vibe-hosting/* về tmp_path"""
    ws = tmp_path / "workspaces"
    logs = tmp_path / "logs"
    backups = tmp_path / "backups"
    uploads = tmp_path / "uploads"
    for d in (ws, logs, backups, uploads):
        d.mkdir()
    config_file = tmp_path / "apps_config.json"

    for mod in (builder_core, main, cicd_core, file_manager_api, hermes_agent_router, archive_builder_core):
        if hasattr(mod, "BASE_WORKSPACE"):
            monkeypatch.setattr(mod, "BASE_WORKSPACE", str(ws))
        if hasattr(mod, "LOGS_DIR"):
            monkeypatch.setattr(mod, "LOGS_DIR", str(logs))
        if hasattr(mod, "CONFIG_DB_FILE"):
            monkeypatch.setattr(mod, "CONFIG_DB_FILE", str(config_file))

    monkeypatch.setattr(main, "LOG_DIR", str(logs))
    monkeypatch.setattr(main, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(main, "UPLOAD_DIR", str(uploads))

    class P:
        pass

    p = P()
    p.root, p.ws, p.logs, p.backups, p.uploads, p.config = tmp_path, ws, logs, backups, uploads, config_file
    return p


class FakeKubectl:
    """
    Giả lập kubectl cho Hermes Verifier. Mặc định mọi workload đều Ready.
    Test có thể đặt `rollout_ok`, `workloads`, `pods`, `logs` để dựng tình huống lỗi.
    """

    def __init__(self):
        self.rollout_ok = True
        self.workloads = {}   # "deployment/x" -> dict JSON (None = không tồn tại)
        self.pods = {}        # selector -> danh sách pod JSON
        self.logs = ""
        self.replicasets = []  # danh sách ReplicaSet JSON cho lịch sử triển khai
        self.calls = []

    def __call__(self, args, timeout=30):
        import json
        import subprocess
        self.calls.append(args)
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        if args[0] == "rollout":
            # rollout_ok có thể là list để mô phỏng nhiều lần poll (vd: [False, True] = đang kéo image rồi Ready)
            ready = self.rollout_ok.pop(0) if isinstance(self.rollout_ok, list) and len(self.rollout_ok) > 1 else (
                self.rollout_ok[0] if isinstance(self.rollout_ok, list) else self.rollout_ok)
            return ok() if ready else subprocess.CompletedProcess(args, 1, "", "timed out waiting for the condition")
        if args[:2] == ["get", "pods"]:
            return ok(json.dumps({"items": self.pods.get(args[3], [])}))
        if args[:2] == ["get", "rs"]:
            return ok(json.dumps({"items": self.replicasets}))
        if args[0] == "get" and args[1] == "events":
            return ok("")
        if args[0] == "get" and args[1] == "deployment,statefulset":
            return ok("\n".join(self.workloads))
        if args[0] == "get":
            name = args[1].split("/")[-1]
            obj = self.workloads.get(args[1], {
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}},
                "status": {"readyReplicas": 1},
            })
            if obj is None:
                return subprocess.CompletedProcess(args, 1, "", f'Error from server (NotFound): "{name}" not found')
            return ok(json.dumps(obj))
        if args[0] == "logs":
            return ok(self.logs)
        return ok()


@pytest.fixture(autouse=True)
def kube(monkeypatch):
    """Không gọi kubectl / Hermes thật trong test"""
    fake = FakeKubectl()
    monkeypatch.setattr(hermes_verifier, "_kubectl", fake)
    monkeypatch.setattr(hermes_verifier, "HERMES_VERIFY_ENABLED", False)
    monkeypatch.setattr(hermes_verifier, "ROLLOUT_TIMEOUT", 0)  # Pod chưa Ready => kết luận ngay, không chờ
    return fake


@pytest.fixture
def client(paths):
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        c.headers.update({"X-API-Key": API_KEY})
        yield c


@pytest.fixture
def no_github(monkeypatch):
    """Không gọi GitHub API thật khi có github_token"""
    monkeypatch.setattr(main, "resolve_github_owner_from_token", lambda token: "tester" if token else None)
