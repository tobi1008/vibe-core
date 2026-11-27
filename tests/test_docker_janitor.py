import subprocess

import docker_janitor
import hermes_verifier


def fake_run(calls, outputs):
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs.pop(0), stderr="")
    return run


def test_cleanup_keeps_cache_and_recent_images(monkeypatch):
    monkeypatch.setenv("DOCKER_JANITOR", "1")
    calls, logs = [], []
    monkeypatch.setattr(docker_janitor.subprocess, "run",
                        fake_run(calls, ["ID\nTotal:\t3.2GB\n", "Deleted: x\nTotal reclaimed space: 2.09GB\n"]))
    docker_janitor.cleanup("shop", lambda app, msg: logs.append(msg))
    assert calls[0][:3] == ["docker", "builder", "prune"] and "--max-used-space" in calls[0]
    assert calls[1][:3] == ["docker", "image", "prune"] and "until=2h" in calls[1]
    assert "builder 3.2GB" in logs[-1] and "image 2.09GB" in logs[-1]


def test_cleanup_quiet_when_nothing_freed_and_disabled(monkeypatch):
    monkeypatch.setenv("DOCKER_JANITOR", "1")
    calls, logs = [], []
    monkeypatch.setattr(docker_janitor.subprocess, "run",
                        fake_run(calls, ["Total:\t0B\n", "Total reclaimed space: 0B\n"]))
    docker_janitor.cleanup("shop", lambda app, msg: logs.append(msg))
    assert logs == []
    monkeypatch.setenv("DOCKER_JANITOR", "0")
    docker_janitor.cleanup("shop", lambda app, msg: logs.append(msg))
    assert len(calls) == 2


def test_cleanup_skips_when_another_run_in_progress(monkeypatch):
    monkeypatch.setenv("DOCKER_JANITOR", "1")
    calls = []
    monkeypatch.setattr(docker_janitor.subprocess, "run", fake_run(calls, []))
    with docker_janitor._lock:
        docker_janitor.cleanup("shop", lambda app, msg: None)
    assert calls == []


def test_finalize_pipeline_triggers_cleanup(kube, monkeypatch, paths):
    started = []
    monkeypatch.setattr(hermes_verifier.docker_janitor, "cleanup_async", lambda app, log: started.append(app))
    hermes_verifier.finalize_pipeline("shop", ["deployment/shop"], error="build failed")
    assert started == ["shop"]
