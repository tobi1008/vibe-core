import json
import subprocess

import pytest

import cicd_core


def git_repo(path, remote):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def last_log_line(paths, app):
    lines = [line for line in (paths.logs / f"{app}.log").read_text().splitlines() if line.strip()]
    return lines[-1]


@pytest.fixture
def rebuilt(monkeypatch):
    calls = []
    monkeypatch.setattr(cicd_core, "update_and_rebuild_component",
                        lambda *a, **kw: calls.append(kw.get("component_name") or a[1]))
    return calls


@pytest.mark.parametrize("raw,expected", [
    ("https://ghp_x@github.com/User/Repo.git", "https://github.com/user/repo"),
    ("https://github.com/u/r/", "https://github.com/u/r"),
    ("", ""),
    (None, ""),
])
def test_get_clean_git_url(raw, expected):
    assert cicd_core.get_clean_git_url(raw) == expected


def test_extract_owner():
    assert cicd_core.extract_owner_from_git_url("https://tok@github.com/Alice/x.git") == "alice"
    assert cicd_core.extract_owner_from_git_url("https://gitlab.com/a/b") is None


def test_redeploy_single_repo(paths, rebuilt):
    git_repo(paths.ws / "shop", "https://github.com/me/shop.git")
    cicd_core.redeploy_app("shop")
    assert rebuilt == ["app"]
    assert last_log_line(paths, "shop") == "DONE"


def test_redeploy_missing_workspace_logs_error(paths):
    with pytest.raises(FileNotFoundError):
        cicd_core.redeploy_app("ghost")
    assert last_log_line(paths, "ghost") == "ERROR"


def test_webhook_from_foreign_owner_is_ignored(paths, rebuilt):
    git_repo(paths.ws / "shop", "https://github.com/me/shop.git")
    paths.config.write_text(json.dumps({"shop": {"primary_github_owner": "me"}}))
    cicd_core.redeploy_app("shop", "main", "https://github.com/original-author/shop.git")
    assert rebuilt == []
    assert "SECURITY IGNORE" in (paths.logs / "shop.log").read_text()


def test_fullstack_rebuilds_only_pushed_component(paths, rebuilt):
    git_repo(paths.ws / "shop-be", "https://github.com/me/shop-api.git")
    git_repo(paths.ws / "shop-fe", "https://github.com/me/shop-ui.git")
    cicd_core.redeploy_app("shop", "main", "https://github.com/me/shop-ui.git")
    assert rebuilt == ["frontend"]


@pytest.mark.xfail(strict=True, reason="BUG: so khớp repo bằng substring => push 'shop-web' rebuild nhầm cả backend 'shop'")
def test_fullstack_repo_name_prefix_does_not_match(paths, rebuilt):
    git_repo(paths.ws / "shop-be", "https://github.com/me/shop.git")
    git_repo(paths.ws / "shop-fe", "https://github.com/me/shop-web.git")
    cicd_core.redeploy_app("shop", "main", "https://github.com/me/shop-web.git")
    assert rebuilt == ["frontend"]


def test_redeploy_failure_writes_error_marker(paths, monkeypatch):
    git_repo(paths.ws / "shop", "https://github.com/me/shop.git")

    def boom(*a, **kw):
        raise Exception("nixpacks build failed")

    monkeypatch.setattr(cicd_core, "update_and_rebuild_component", boom)
    try:
        cicd_core.redeploy_app("shop")
    except Exception:
        pass
    assert last_log_line(paths, "shop") == "ERROR"


def test_redeploy_verifies_rebuilt_deployments(paths, rebuilt, kube):
    git_repo(paths.ws / "shop-be", "https://github.com/me/shop-api.git")
    git_repo(paths.ws / "shop-fe", "https://github.com/me/shop-ui.git")
    cicd_core.redeploy_app("shop", target_component="be")
    rollouts = [c[2] for c in kube.calls if c[0] == "rollout"]
    assert rollouts == ["deployment/shop-be"]
    assert last_log_line(paths, "shop") == "DONE"
