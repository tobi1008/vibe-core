import json

import pytest

import builder_core
import hermes_verifier


def pod(name, reason=None, ready=False, restarts=0, phase="Running", last_reason=None):
    state = {"waiting": {"reason": reason, "message": f"{reason} detail"}} if reason else {"running": {}}
    cs = {"name": "app", "ready": ready, "restartCount": restarts, "state": state}
    if last_reason:
        cs["lastState"] = {"terminated": {"reason": last_reason, "exitCode": 1}}
    return {"metadata": {"name": name}, "status": {"phase": phase, "containerStatuses": [cs]}}


def not_ready_deployment(name):
    return {"spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}}, "status": {}}


def log_lines(paths, app):
    return [line for line in (paths.logs / f"{app}.log").read_text().splitlines() if line.strip()]


def test_healthy_pipeline_writes_success_lines_then_done(paths):
    ok = hermes_verifier.finalize_pipeline("shop", ["deployment/shop"], success_lines=["=== [HOÀN TẤT] ok ==="])
    assert ok
    lines = log_lines(paths, "shop")
    assert lines[-2:] == ["=== [HOÀN TẤT] ok ===", "DONE"]
    assert any("Hoạt động ổn định" in line for line in lines)
    report = json.loads((paths.logs / "shop.health.json").read_text())
    assert report["status"] == "healthy" and report["analyzed_by"] == "rule-based"


def test_crashloop_is_code_error_and_marks_error(paths, kube):
    kube.rollout_ok = False
    kube.workloads = {"deployment/shop": not_ready_deployment("shop")}
    kube.pods = {"app=shop": [pod("shop-1", "CrashLoopBackOff", restarts=4, last_reason="Error")]}
    kube.logs = "TypeError: Cannot read properties of undefined (reading 'DB_HOST')"

    ok = hermes_verifier.finalize_pipeline("shop", ["deployment/shop"], success_lines=["=== [HOÀN TẤT] ok ==="])
    assert not ok
    lines = log_lines(paths, "shop")
    assert lines[-1] == "ERROR"
    assert not any("[HOÀN TẤT]" in line for line in lines)  # FE không được hiểu nhầm là thành công
    report = hermes_verifier.load_report("shop")
    assert report["status"] == "code_error"
    assert report["workloads"][0]["pods"][0]["containers"][0]["restarts"] == 4
    assert ["logs", "shop-1", "--all-containers", "--previous", "--tail=60"] in kube.calls


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "CreateContainerConfigError"])
def test_image_or_config_problem_is_k8s_config_error(paths, kube, reason):
    kube.rollout_ok = False
    kube.workloads = {"deployment/shop": not_ready_deployment("shop")}
    kube.pods = {"app=shop": [pod("shop-1", reason)]}
    report = hermes_verifier.verify_deployment("shop", ["deployment/shop"])
    assert report["status"] == "k8s_config_error" and not report["ready"]


def test_missing_workload_is_k8s_config_error(paths, kube):
    kube.rollout_ok = False
    kube.workloads = {"deployment/shop": None}
    report = hermes_verifier.verify_deployment("shop", ["deployment/shop"])
    assert report["status"] == "k8s_config_error"
    assert report["workloads"][0]["ready"] == "không tồn tại"


def test_pipeline_error_skips_rollout_wait(paths, kube):
    report = hermes_verifier.verify_deployment("shop", ["deployment/shop"], error="docker push failed: https://ghp_abcdefghijklmnopqrstuvwxyz@x")
    assert report["status"] == "pipeline_error" and not report["ready"]
    assert not any(c[0] == "rollout" for c in kube.calls)
    assert "ghp_" not in report["pipeline_error"]


def test_hermes_explains_failure(paths, kube, monkeypatch):
    kube.rollout_ok = False
    kube.workloads = {"deployment/shop": not_ready_deployment("shop")}
    kube.pods = {"app=shop": [pod("shop-1", "CrashLoopBackOff", restarts=2)]}
    prompts = []

    def fake_hermes(prompt, **kw):
        prompts.append((prompt, kw))
        return {
            "status": "k8s_config_error",
            "summary": "Container lắng nghe cổng 3000 nhưng Service trỏ 8080",
            "evidence": ["Server listening on 3000"],
            "suggestions": ["Đặt PORT=8080 hoặc sửa targetPort"],
        }

    monkeypatch.setattr(hermes_verifier, "HERMES_VERIFY_ENABLED", True)
    monkeypatch.setattr(hermes_verifier, "run_hermes_json", fake_hermes)
    hermes_verifier.finalize_pipeline("shop", ["deployment/shop"])

    report = hermes_verifier.load_report("shop")
    assert report["analyzed_by"] == "hermes" and report["status"] == "k8s_config_error"
    assert prompts[0][1]["max_models"] == hermes_verifier.HERMES_VERIFY_MAX_MODELS
    text = (paths.logs / "shop.log").read_text()
    assert "cổng 3000" in text and "Gợi ý      : - Đặt PORT=8080" in text


def test_hermes_cannot_mark_unready_app_as_healthy(paths, kube, monkeypatch):
    kube.rollout_ok = False
    kube.workloads = {"deployment/shop": not_ready_deployment("shop")}
    monkeypatch.setattr(hermes_verifier, "HERMES_VERIFY_ENABLED", True)
    monkeypatch.setattr(hermes_verifier, "run_hermes_json", lambda *a, **kw: {"status": "healthy", "summary": "ok"})
    assert not hermes_verifier.finalize_pipeline("shop", ["deployment/shop"])
    assert hermes_verifier.load_report("shop")["status"] == "starting"


def test_hermes_unavailable_falls_back_to_rules(paths, kube, monkeypatch):
    def down(*a, **kw):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(hermes_verifier, "HERMES_VERIFY_ENABLED", True)
    monkeypatch.setattr(builder_core, "run_hermes_json", down)
    monkeypatch.setattr(hermes_verifier, "run_hermes_json", down)
    assert hermes_verifier.finalize_pipeline("shop", ["deployment/shop"])
    assert hermes_verifier.load_report("shop")["analyzed_by"] == "rule-based"


def test_verifier_crash_does_not_break_pipeline(paths, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(hermes_verifier, "verify_deployment", broken)
    assert hermes_verifier.finalize_pipeline("shop", ["deployment/shop"])
    assert log_lines(paths, "shop")[-1] == "DONE"
