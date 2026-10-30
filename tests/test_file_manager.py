import os

import pytest


@pytest.fixture
def project(paths):
    ws = paths.ws / "shop"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.js").write_text("console.log(1)")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("url = https://ghp_secret@github.com/u/r")
    (ws / "node_modules").mkdir()
    return ws


def test_file_tree_hides_git_and_node_modules(client, project):
    tree = client.get("/api/project/shop/files").json()["tree"]
    names = [n["name"] for n in tree]
    assert names == ["src"]
    assert tree[0]["children"][0]["path"] == "src/app.js"


def test_read_and_write_file(client, project):
    res = client.get("/api/project/shop/file", params={"path": "src/app.js"})
    assert res.json()["content"] == "console.log(1)"

    res = client.put("/api/project/shop/file", json={"path": "src/new/x.txt", "content": "hi"})
    assert res.status_code == 200
    assert (project / "src" / "new" / "x.txt").read_text() == "hi"


@pytest.mark.parametrize("path", ["../other/secret", ".git/config", "src/../../shop2/x"])
def test_read_outside_workspace_or_git_is_forbidden(client, project, path):
    assert client.get("/api/project/shop/file", params={"path": path}).status_code == 403


def test_absolute_path_is_resolved_inside_workspace(client, project):
    # "/etc/passwd" được hiểu là <workspace>/etc/passwd chứ không phải file hệ thống
    assert client.get("/api/project/shop/file", params={"path": "/etc/passwd"}).status_code == 404


def test_write_outside_workspace_is_forbidden(client, project, paths):
    res = client.put("/api/project/shop/file", json={"path": "../pwned.txt", "content": "x"})
    assert res.status_code == 403
    assert not (paths.ws / "pwned.txt").exists()


def test_unknown_project_404(client, paths):
    assert client.get("/api/project/ghost/files").status_code == 404


def test_fullstack_paths_map_to_be_and_fe(client, paths):
    (paths.ws / "shop-be").mkdir()
    (paths.ws / "shop-fe").mkdir()
    (paths.ws / "shop-be" / "server.js").write_text("be")
    (paths.ws / "shop-fe" / "index.html").write_text("fe")

    tree = client.get("/api/project/shop/files").json()["tree"]
    assert [n["name"] for n in tree] == ["backend", "frontend"]
    assert client.get("/api/project/shop/file", params={"path": "backend/server.js"}).json()["content"] == "be"
    assert client.get("/api/project/shop/file", params={"path": "frontend/index.html"}).json()["content"] == "fe"


def test_symlink_escape_is_forbidden(client, project, paths):
    secret = paths.root / "server.env"
    secret.write_text("VIBE_API_KEY=leak")
    os.symlink(secret, project / "src" / "link.txt")  # repo có thể chứa sẵn symlink như vậy
    assert client.get("/api/project/shop/file", params={"path": "src/link.txt"}).status_code == 403
