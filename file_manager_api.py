import os
import json
import requests
from git import Repo
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from builder_core import BASE_WORKSPACE
from cicd_core import redeploy_app

router = APIRouter(prefix="/api/project", tags=["File Manager & Git"])
CONFIG_DB_FILE = "/opt/vibe-hosting/apps_config.json"

# --- DATA SCHEMAS ---
class SaveFilePayload(BaseModel):
    path: str
    content: str

class PushGitPayload(BaseModel):
    githubToken: str
    commitMessage: Optional[str] = "Update via Vibe Hosting File Manager"
    repoName: Optional[str] = None       # Cho phép user chỉ định tên repo (mặc định lấy tên repo hiện tại)
    targetBranch: Optional[str] = "main" # Nhánh mục tiêu (mặc định main)
    component: Optional[str] = None      # 'be', 'fe', hoặc None (cả hai)
    autoRebuild: Optional[bool] = True   # Tự động kích hoạt CI/CD rebuild ngay sau khi push


def update_persisted_app_config(app_name: str, updates: dict):
    """Cập nhật cấu hình lưu trữ vĩnh viễn trong apps_config.json"""
    data = {}
    if os.path.exists(CONFIG_DB_FILE):
        try:
            with open(CONFIG_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    current_cfg = data.get(app_name, {"auto_cicd": True})
    current_cfg.update(updates)
    data[app_name] = current_cfg

    try:
        with open(CONFIG_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ [WARN] Không thể ghi apps_config.json: {e}")


def resolve_project_workspace(project_name: str) -> str:
    """Xác định đường dẫn thư mục dự án"""
    candidate_paths = [
        os.path.join(BASE_WORKSPACE, f"{project_name}-monorepo"),
        os.path.join(BASE_WORKSPACE, project_name),
        os.path.join(BASE_WORKSPACE, f"{project_name}-be"),
        os.path.join(BASE_WORKSPACE, f"{project_name}-fe"),
    ]
    for p in candidate_paths:
        if os.path.exists(p) and os.path.isdir(p):
            return os.path.abspath(p)
    raise HTTPException(
        status_code=404,
        detail=f"Không tìm thấy thư mục workspace cho dự án: '{project_name}'"
    )


def validate_safe_path(base_dir: str, target_rel_path: str) -> str:
    """Ngăn chặn lỗ hổng Directory Traversal"""
    full_path = os.path.abspath(os.path.join(base_dir, target_rel_path))
    if not full_path.startswith(base_dir):
        raise HTTPException(status_code=403, detail="Từ chối truy cập: Đường dẫn nằm ngoài workspace dự án.")
    return full_path


def resolve_fullstack_subpath(project_name: str, target_path: str) -> tuple[str, str]:
    """Ánh xạ đường dẫn cho dự án Fullstack 2-repo"""
    ws_be = os.path.join(BASE_WORKSPACE, f"{project_name}-be")
    ws_fe = os.path.join(BASE_WORKSPACE, f"{project_name}-fe")

    clean_path = target_path.strip("/\\")

    if os.path.exists(ws_be) or os.path.exists(ws_fe):
        if clean_path == "backend" or clean_path.startswith("backend/"):
            rel = clean_path[len("backend/"):].strip("/\\") if clean_path.startswith("backend/") else ""
            return ws_be, rel
        elif clean_path == "frontend" or clean_path.startswith("frontend/"):
            rel = clean_path[len("frontend/"):].strip("/\\") if clean_path.startswith("frontend/") else ""
            return ws_fe, rel

    base_dir = resolve_project_workspace(project_name)
    return base_dir, clean_path


# --- 1. API: LẤY CÂY THƯ MỤC DỰ ÁN ---
@router.get("/{project_name}/files")
def get_file_tree(project_name: str):
    ignore_patterns = {
        ".git", "node_modules", ".next", "dist", "build",
        ".cache", "__pycache__", ".turbo", ".vercel"
    }

    ws_be = os.path.join(BASE_WORKSPACE, f"{project_name}-be")
    ws_fe = os.path.join(BASE_WORKSPACE, f"{project_name}-fe")

    def _build_tree(current_dir: str, root_dir: str, path_prefix: str = ""):
        items = []
        try:
            for entry in os.scandir(current_dir):
                if entry.name in ignore_patterns:
                    continue
                
                rel_path = os.path.relpath(entry.path, root_dir)
                full_rel_path = os.path.join(path_prefix, rel_path) if path_prefix else rel_path
                is_directory = entry.is_dir(follow_symlinks=False)

                node = {
                    "name": entry.name,
                    "path": full_rel_path.replace("\\", "/"),
                    "type": "directory" if is_directory else "file",
                }

                if is_directory:
                    node["children"] = _build_tree(entry.path, root_dir, path_prefix)

                items.append(node)
        except PermissionError:
            pass

        return sorted(items, key=lambda x: (x["type"] != "directory", x["name"].lower()))

    if os.path.exists(ws_be) and os.path.exists(ws_fe):
        tree = [
            {
                "name": "backend",
                "path": "backend",
                "type": "directory",
                "children": _build_tree(ws_be, ws_be, "backend")
            },
            {
                "name": "frontend",
                "path": "frontend",
                "type": "directory",
                "children": _build_tree(ws_fe, ws_fe, "frontend")
            }
        ]
        return {"status": "success", "project": project_name, "tree": tree}

    project_dir = resolve_project_workspace(project_name)
    return {
        "status": "success",
        "project": project_name,
        "tree": _build_tree(project_dir, project_dir),
    }


# --- 2. API: ĐỌC NỘI DUNG FILE ---
@router.get("/{project_name}/file")
def read_file_content(project_name: str, path: str = Query(..., description="Đường dẫn tương đối của file")):
    base_dir, rel_path = resolve_fullstack_subpath(project_name, path)
    target_file = validate_safe_path(base_dir, rel_path)

    if not os.path.exists(target_file) or not os.path.isfile(target_file):
        raise HTTPException(status_code=404, detail="File không tồn tại hoặc là thư mục.")

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            content = f.read()
        return {
            "status": "success",
            "path": path,
            "content": content
        }
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="File dạng nhị phân (Binary) hoặc mã hóa không hỗ trợ xem trực tiếp."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi đọc file: {str(e)}")


# --- 3. API: LƯU NỘI DUNG FILE ---
@router.put("/{project_name}/file")
def save_file_content(project_name: str, payload: SaveFilePayload):
    base_dir, rel_path = resolve_fullstack_subpath(project_name, payload.path)
    target_file = validate_safe_path(base_dir, rel_path)

    try:
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(payload.content)
        return {
            "status": "success",
            "message": f"Đã lưu thành công file '{payload.path}'",
            "path": payload.path
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi lưu file: {str(e)}")


# --- 4. API: SMART PUSH LÊN GITHUB CÁ NHÂN & ƯU TIÊN CI/CD ---
@router.post("/{project_name}/github/push")
def push_changes_to_github(
    project_name: str,
    payload: PushGitPayload,
    background_tasks: BackgroundTasks
):
    token = payload.githubToken.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Thiếu GitHub Personal Access Token.")

    # 1. Xác thực Token và lấy username chính chủ
    gh_headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    user_res = requests.get("https://api.github.com/user", headers=gh_headers, timeout=15)
    if user_res.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail="GitHub Token không hợp lệ hoặc đã hết hạn. Vui lòng kiểm tra lại token có quyền 'repo'."
        )

    username = user_res.json().get("login")

    # 2. Xác định danh sách workspace cần push
    ws_be = os.path.join(BASE_WORKSPACE, f"{project_name}-be")
    ws_fe = os.path.join(BASE_WORKSPACE, f"{project_name}-fe")

    repos_to_process = []
    norm_comp = (payload.component or "").lower().strip()

    if os.path.exists(ws_be) or os.path.exists(ws_fe):
        if os.path.exists(ws_be) and norm_comp in ["be", "backend", ""]:
            repos_to_process.append(("backend", ws_be))
        if os.path.exists(ws_fe) and norm_comp in ["fe", "frontend", ""]:
            repos_to_process.append(("frontend", ws_fe))
    else:
        project_dir = resolve_project_workspace(project_name)
        repos_to_process.append(("app", project_dir))

    target_branch = payload.targetBranch or "main"
    commit_msg = payload.commitMessage or "Update via Vibe Hosting File Manager"
    results = []
    config_updates = {
        "primary_github_owner": username,
        "last_pushed_branch": target_branch,
    }

    for comp_label, p_dir in repos_to_process:
        try:
            repo = Repo(p_dir)
            current_remote = repo.remotes.origin.url

            # Trích xuất tên repo hiện tại
            clean_repo_name = current_remote.split("/")[-1].replace(".git", "").strip()
            if payload.repoName:
                clean_repo_name = payload.repoName.strip()

            # 3. Kiểm tra hoặc tự động tạo repository trên tài khoản cá nhân
            repo_check_url = f"https://api.github.com/repos/{username}/{clean_repo_name}"
            check_res = requests.get(repo_check_url, headers=gh_headers, timeout=10)

            if check_res.status_code == 404:
                create_res = requests.post(
                    "https://api.github.com/user/repos",
                    headers=gh_headers,
                    json={
                        "name": clean_repo_name,
                        "private": True,
                        "description": f"Repository đồng bộ từ Vibe Hosting ({project_name})"
                    },
                    timeout=15
                )
                if create_res.status_code not in [200, 201]:
                    raise Exception(f"Không thể tự động tạo repository trên GitHub {username}: {create_res.text}")

            # 4. CẬP NHẬT REMOTE VĨNH VIỄN TRỎ VỀ REPO CỦA USER
            new_origin_url = f"https://{token}@github.com/{username}/{clean_repo_name}.git"
            public_repo_url = f"https://github.com/{username}/{clean_repo_name}"
            repo.git.remote("set-url", "origin", new_origin_url)

            # 5. Đồng bộ tên nhánh (Ví dụ master -> main)
            active_branch = repo.active_branch.name
            if active_branch != target_branch:
                try:
                    repo.git.branch("-M", target_branch)
                except Exception:
                    pass

            # 6. Cam kết các thay đổi chưa commit
            repo.git.add(A=True)
            if repo.is_dirty() or repo.untracked_files:
                repo.index.commit(commit_msg)

            # 7. Push code lên GitHub của user
            repo.git.push("-u", "origin", target_branch, "--force")

            # Lưu link repo vào cấu hình ứng dụng để CI/CD nhận diện
            if comp_label == "backend":
                config_updates["be_repo_url"] = public_repo_url
            elif comp_label == "frontend":
                config_updates["fe_repo_url"] = public_repo_url
            else:
                config_updates["repo_url"] = public_repo_url

            results.append({
                "component": comp_label,
                "status": "success",
                "repo_url": public_repo_url,
                "branch": target_branch,
                "owner": username,
                "message": f"[{comp_label.upper()}] Đã chuyển remote và push thành công lên {public_repo_url} (nhánh {target_branch})"
            })

        except Exception as e:
            results.append({
                "component": comp_label,
                "status": "error",
                "message": f"[{comp_label.upper()}] Thất bại: {str(e)}"
            })

    # Cập nhật apps_config.json vĩnh viễn
    update_persisted_app_config(project_name, config_updates)

    # 8. TỰ ĐỘNG KÍCH HOẠT CI/CD REBUILD TỪ LINK GITHUB VỪA PUSH
    if payload.autoRebuild and not any(r["status"] == "error" for r in results):
        target_comp = norm_comp if norm_comp in ["be", "fe"] else None
        background_tasks.add_task(
            redeploy_app,
            app_name=project_name,
            branch=target_branch,
            triggered_repo_url=None, # None để ép CI/CD ưu tiên origin của tài khoản vừa push
            pull_git=True,           # Kéo mã nguồn mới nhất vừa push từ GitHub của user kết nối
            target_component=target_comp
        )

    has_error = any(r["status"] == "error" for r in results)
    rebuild_note = f" Đang tự động Rebuild Pod từ GitHub của tài khoản [{username}]..." if payload.autoRebuild and not has_error else ""

    return {
        "status": "partial_error" if has_error else "success",
        "user": username,
        "rebuild_triggered": payload.autoRebuild and not has_error,
        "results": results,
        "message": " | ".join(r["message"] for r in results) + rebuild_note
    }
