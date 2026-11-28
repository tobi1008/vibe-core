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


def test_tick_periodic_and_disk_full(monkeypatch):
    monkeypatch.setenv("DOCKER_JANITOR", "1")
    monkeypatch.setattr(docker_janitor, "_last_aggressive", 0.0)
    runs, logs = [], []
    monkeypatch.setattr(docker_janitor, "cleanup",
                        lambda app, log, aggressive=False: runs.append(aggressive))
    log = lambda name, msg: logs.append(msg)
    hours = docker_janitor.INTERVAL_SECONDS

    monkeypatch.setattr(docker_janitor, "disk_percent", lambda path="/": 40.0)
    assert docker_janitor.tick(hours + 1, 0.0, log) == hours + 1 and runs == [False]
    assert docker_janitor.tick(hours + 2, hours + 1, log) == hours + 1 and runs == [False]

    monkeypatch.setattr(docker_janitor, "disk_percent", lambda path="/": 92.0)
    docker_janitor.tick(hours + 10, hours + 1, log)
    assert runs == [False, True] and "92%" in logs[-1]
    # Ổ vẫn đầy (vì thứ khác): không dọn mạnh lại mỗi 5 phút
    docker_janitor.tick(hours + 310, hours + 10, log)
    assert runs == [False, True]


def test_janitor_disabled_starts_nothing(monkeypatch):
    import threading
    monkeypatch.setenv("DOCKER_JANITOR", "0")
    assert not docker_janitor.start_background(threading.Event(), lambda *a: None)
