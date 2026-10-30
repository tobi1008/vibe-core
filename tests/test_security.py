import hashlib
import os
import hmac

import pytest
from fastapi import HTTPException

import security


@pytest.mark.parametrize("name", ["app", "my-app", "a1", "x" * 40, "0shop"])
def test_validate_app_name_accepts_dns_labels(name):
    assert security.validate_app_name(name) == name


@pytest.mark.parametrize("name", [
    "", None, "App", "my_app", "-app", "app-", "x" * 41,
    "a;rm -rf /", "a b", "../etc",
])
def test_validate_app_name_rejects_invalid(name):
    with pytest.raises(HTTPException) as exc:
        security.validate_app_name(name)
    assert exc.value.status_code == 400


def test_validate_app_name_rejects_trailing_newline():
    with pytest.raises(HTTPException):
        security.validate_app_name("app\n")


def test_validate_repo_url_accepts_https():
    assert security.validate_repo_url("  https://github.com/u/r.git ") == "https://github.com/u/r.git"


@pytest.mark.parametrize("url", [
    "http://github.com/u/r", "file:///etc", "/tmp/repo", "git@github.com:u/r.git",
    "https://github.com/u/r --upload-pack=touch", "", None,
])
def test_validate_repo_url_rejects(url):
    with pytest.raises(HTTPException):
        security.validate_repo_url(url)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_github_signature_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(security, "GITHUB_WEBHOOK_SECRET", "s3cret")
    body = b'{"ref":"refs/heads/main"}'
    assert security.verify_github_signature(body, _sign("s3cret", body))
    assert not security.verify_github_signature(body, _sign("wrong", body))
    assert not security.verify_github_signature(body, None)
    assert not security.verify_github_signature(body, "sha1=abc")


def test_github_signature_skipped_without_secret(monkeypatch):
    # Hành vi hiện tại: không cấu hình secret => chấp nhận mọi request
    monkeypatch.setattr(security, "GITHUB_WEBHOOK_SECRET", "")
    assert security.verify_github_signature(b"{}", None)


def test_redact_secrets():
    text = (
        "clone https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/u/r.git "
        "and https://user:pass@example.com/x token ghp_ABCDEFGHIJKLMNOPQRSTUVWX "
        "pat github_pat_11ABCDEFG0123456789_abcdefghij"
    )
    out = security.redact_secrets(text)
    assert "ghp_" not in out
    assert "user:pass" not in out
    assert "github_pat_" not in out
    assert "https://***@github.com/u/r.git" in out
    assert security.redact_secrets("") == ""


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"),
    ("backup.sql", "backup.sql"),
    ("a b;c$(id).sql", "a_b_c__id_.sql"),
    (".env", "env"),
    ("", "upload"),
    (None, "upload"),
    ("...", "upload"),
])
def test_safe_upload_filename(raw, expected):
    assert security.safe_upload_filename(raw) == expected


def test_is_inside_dir():
    assert security.is_inside_dir("/base", "/base")
    assert security.is_inside_dir("/base", "/base/a/b")
    assert not security.is_inside_dir("/base", "/base-evil/a")
    assert not security.is_inside_dir("/base", "/base/../etc")


@pytest.mark.parametrize("path,expected", [
    (".git/config", True), ("a/.git/HEAD", True), ("a\\.git\\config", True),
    (".github/workflows/x.yml", False), ("src/git.py", False), ("", False),
])
def test_touches_git_dir(path, expected):
    assert security.touches_git_dir(path) is expected


def test_safe_join(tmp_path):
    base = tmp_path / "ws"
    (base / "src").mkdir(parents=True)
    os.symlink("/etc", base / "etc-link")
    assert security.safe_join(str(base), "src/vite.config.js") == str(base / "src/vite.config.js")
    assert security.safe_join(str(base), "new/dir/file.txt") is not None
    for bad in ["../escape.txt", "/root/.ssh/authorized_keys", ".git/config", "etc-link/passwd", "", ".", None]:
        assert security.safe_join(str(base), bad) is None, bad
