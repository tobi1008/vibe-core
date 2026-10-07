import os
import json
import subprocess
import time
import requests
from typing import Optional

from builder_core import (
    BASE_WORKSPACE,
    DOCKER_USER,
    write_log,
    run_cmd,
)

try:
    from db_builder_core import (
        auto_patch_nodejs_esm_conflicts,
        link_frontend_to_backend,
    )
except ImportError:
    auto_patch_nodejs_esm_conflicts = None
    link_frontend_to_backend = None

try:
    from builder_core import K8S_NAMESPACE
except ImportError:
    K8S_NAMESPACE = "default"

CONFIG_DB_FILE = "/opt/vibe-hosting/apps_config.json"

def build_nixpacks_for_cicd(target_dir: str, image_tag: str, app_name: str):
    """
    Hàm đóng gói Nixpacks cô lập hoàn toàn bên trong CI/CD:
    - Không can thiệp hay phụ thuộc vào db_builder_core.py
    - Tự động thay thế 'npm ci' bằng 'npm install --legacy-peer-deps' để không bị lỗi EUSAGE khi lệch package-lock.json
    - Tự nâng cấp Node.js lên 20 nếu phát hiện dependencies hiện đại (Firebase, Vite, v.v.)
    """
    write_log(app_name, f"   📦 [CI/CD NIXPACKS] Chuẩn bị đóng gói Docker Image: {image_tag}...")
    nixpacks_cmd = ["nixpacks", "build", ".", "--name", image_tag]

    pkg_json_file = os.path.join(target_dir, "package.json")
    if os.path.exists(pkg_json_file):
        is_yarn = os.path.exists(os.path.join(target_dir, "yarn.lock"))
        is_pnpm = os.path.exists(os.path.join(target_dir, "pnpm-lock.yaml"))
        is_bun = os.path.exists(os.path.join(target_dir, "bun.lockb"))

        # Sử dụng npm install thay vì npm ci để tránh vấp lỗi EUSAGE do lệch lockfile
        if not (is_yarn or is_pnpm or is_bun):
            nixpacks_cmd.extend(["--env", "NIXPACKS_INSTALL_CMD=npm install --legacy-peer-deps"])

        # Nâng cấp Node lên 20 nếu có yêu cầu từ package
        try:
            with open(pkg_json_file, "r", encoding="utf-8") as pf:
                raw_pkg = pf.read().lower()
                if any(kw in raw_pkg for kw in ["firebase", ">=20", "engines", "next", "vite"]):
                    nixpacks_cmd.extend(["--env", "NIXPACKS_NODE_VERSION=20"])
        except Exception:
            pass

    run_cmd(nixpacks_cmd, app_name, cwd=target_dir)

def get_clean_git_url(url: str) -> str:
    """Làm sạch URL GitHub để so khớp chính xác (loại bỏ token auth, dấu slash và đuôi .git)"""
    if not url:
        return ""
    clean = url.strip().rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    if "@github.com" in clean:
        clean = "https://github.com/" + clean.split("@github.com/")[-1]
    return clean.lower()


def extract_owner_from_git_url(url: str) -> Optional[str]:
    """Trích xuất động GitHub username/org từ URL bất kỳ (không gắn cứng tài khoản)"""
    clean = get_clean_git_url(url)
    if "github.com/" in clean:
        parts = clean.split("github.com/")[-1].split("/")
        if len(parts) >= 1 and parts[0]:
            return parts[0].lower()
    return None


def get_repo_remote_url(repo_dir: str) -> Optional[str]:
    """Lấy URL remote origin từ cấu hình thư mục git"""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True
        )
        return res.stdout.strip()
    except Exception:
        return None


def get_active_app_config(app_name: str) -> dict:
    """Đọc cấu hình hiện tại của ứng dụng từ file DB"""
    if os.path.exists(CONFIG_DB_FILE):
        try:
            with open(CONFIG_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get(app_name, {})
        except Exception:
            pass
    return {}


def sync_repo_remote_to_user(
    target_dir: str,
    expected_repo_url: Optional[str],
    primary_owner: Optional[str],
    app_name: str
) -> Optional[str]:
    """
    TỰ ĐỘNG HÓA 100%:
    Kiểm tra xem tài khoản cá nhân sở hữu Token đã có repository này hay chưa.
    Nếu tài khoản cá nhân đã có repo (do fork, push từ File Manager hoặc push từ FE),
    hệ thống tự động chuyển vĩnh viễn remote sang tài khoản cá nhân và đổi nhánh sang main.
    """
    current_remote = get_repo_remote_url(target_dir) or ""
    token = ""
    if "@github.com" in current_remote and "https://" in current_remote:
        try:
            token = current_remote.split("https://")[1].split("@github.com")[0]
        except Exception:
            token = ""

    current_clean = get_clean_git_url(current_remote)
    current_owner = extract_owner_from_git_url(current_clean)

    # 1. Tự động nhận diện chủ tài khoản từ token nếu primary_owner chưa được set
    detected_owner = primary_owner
    if not detected_owner and token:
        try:
            gh_res = requests.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {token}"},
                timeout=5
            )
            if gh_res.status_code == 200:
                detected_owner = gh_res.json().get("login")
        except Exception:
            pass

    # 2. Xác định tên repository
    repo_name = ""
    if "/" in current_clean:
        repo_name = current_clean.split("/")[-1].replace(".git", "")

    new_remote_url = None
    if expected_repo_url:
        clean_expected = get_clean_git_url(expected_repo_url)
        if current_clean != clean_expected:
            path_part = clean_expected.replace("https://github.com/", "").strip("/")
            new_remote_url = f"https://{token + '@' if token else ''}github.com/{path_part}.git"
    elif detected_owner and current_owner and current_owner.lower() != detected_owner.lower() and repo_name:
        # Kiểm tra xem repo đã tồn tại trên tài khoản của user chưa qua GitHub API
        headers = {"Authorization": f"token {token}"} if token else {}
        check_url = f"https://api.github.com/repos/{detected_owner}/{repo_name}"
        try:
            res = requests.get(check_url, headers=headers, timeout=5)
            if res.status_code == 200:
                new_remote_url = f"https://{token + '@' if token else ''}github.com/{detected_owner}/{repo_name}.git"
                write_log(app_name, f"   ✨ [AUTO-MIGRATION] Phát hiện kho cá nhân https://github.com/{detected_owner}/{repo_name} đã sẵn sàng!")
        except Exception:
            pass

    # 3. Tiến hành cập nhật remote và đổi nhánh tự động nếu phát hiện kho mới
    if new_remote_url:
        try:
            subprocess.run(
                ["git", "remote", "set-url", "origin", new_remote_url],
                cwd=target_dir, check=True, capture_output=True
            )
            clean_display = get_clean_git_url(new_remote_url)
            write_log(app_name, f"   🔄 [REMOTE SYNC] Đã tự động chuyển Remote Origin sang kho cá nhân: {clean_display}")

            # Đổi nhánh local sang main nếu đang là master
            try:
                cur_b = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target_dir, text=True).strip()
                if cur_b != "main":
                    subprocess.run(["git", "branch", "-M", "main"], cwd=target_dir, check=False)
            except Exception:
                pass

            return new_remote_url
        except Exception as e:
            write_log(app_name, f"   ⚠️ [REMOTE SYNC] Không thể chuyển remote: {e}")

    return current_remote


def update_and_rebuild_component(
    app_name: str,
    component_name: str,
    target_dir: str,
    branch: str,
    deploy_name: str,
    image_base: str,
    pull_git: bool = True
):
    commit_hash = str(int(time.time()))
    new_image_tag = f"{image_base}:{commit_hash}"

    # 0. Tự động kiểm tra và chuyển remote sang kho cá nhân đã liên kết nếu cần
    app_cfg = get_active_app_config(app_name)
    primary_owner = (app_cfg.get("primary_github_owner") or "").lower()
    expected_repo_url = app_cfg.get(f"{component_name}_repo_url") or (app_cfg.get("repo_url") if component_name == "app" else None)

    active_remote = sync_repo_remote_to_user(
        target_dir=target_dir,
        expected_repo_url=expected_repo_url,
        primary_owner=primary_owner,
        app_name=app_name
    ) or get_repo_remote_url(target_dir) or "Chưa rõ"

    clean_display_url = get_clean_git_url(active_remote)

    write_log(app_name, f"\n🚀 [CI/CD - {component_name.upper()}] Bắt đầu quy trình đóng gói tại: {target_dir}")
    write_log(app_name, f"   📌 Nguồn mã nguồn ưu tiên: {clean_display_url}")

    # 1. Cập nhật Git từ Remote Origin hiện tại
    if pull_git:
        write_log(app_name, f"   [1] Đang đồng bộ và fetch mã nguồn từ remote origin...")
        run_cmd(["git", "fetch", "--all"], app_name, cwd=target_dir)

        # Tự động nhận diện nhánh ưu tiên (main > master > branch chỉ định)
        target_branch = branch
        try:
            remote_branches = subprocess.check_output(
                ["git", "branch", "-r"],
                cwd=target_dir, text=True
            )
            if "origin/main" in remote_branches and branch in ["main", "master"]:
                target_branch = "main"
            elif "origin/master" in remote_branches and branch in ["main", "master"] and "origin/main" not in remote_branches:
                target_branch = "master"

            # Đổi nhánh local tương ứng
            current_local_branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=target_dir, text=True
            ).strip()
            if current_local_branch != target_branch:
                subprocess.run(["git", "branch", "-M", target_branch], cwd=target_dir, check=False)
        except Exception:
            pass

        write_log(app_name, f"   -> Reset mã nguồn theo nhánh: origin/{target_branch}")
        try:
            run_cmd(["git", "reset", "--hard", f"origin/{target_branch}"], app_name, cwd=target_dir)
        except Exception:
            run_cmd(["git", "reset", "--hard", f"origin/HEAD"], app_name, cwd=target_dir)
    else:
        write_log(app_name, "   [1] [AUTO-HEAL MODE] Giữ nguyên mã nguồn sửa đổi cục bộ, bỏ qua pull git.")

    # [TỰ ĐỘNG VÁ CODE DÙNG CHUNG TỪ DB_BUILDER_CORE]
    if component_name in ["backend", "app"] and auto_patch_nodejs_esm_conflicts:
        write_log(app_name, f"   🔍 [AUTO-PATCH] Quét và chuẩn hóa CommonJS/ESM cho {component_name}...")
        try:
            auto_patch_nodejs_esm_conflicts(target_dir, app_name)
        except Exception as e:
            write_log(app_name, f"   ⚠️ Lỗi auto_patch_nodejs_esm_conflicts: {e}")

    elif component_name == "frontend" and link_frontend_to_backend:
        be_domain = app_cfg.get("be_domain")
        if be_domain:
            write_log(app_name, f"   🔗 [FE LINKER] Đảm bảo cấu hình Backend API (https://{be_domain})...")
            try:
                link_frontend_to_backend(target_dir, app_name, be_domain)
            except Exception as e:
                write_log(app_name, f"   ⚠️ Lỗi link_frontend_to_backend: {e}")

    # 2. Đóng gói Docker Image bằng Nixpacks chuẩn hóa dùng chung
    write_log(app_name, f"   [2] Đang đóng gói Docker Image phiên bản mới: {new_image_tag}...")
    if build_nixpacks_for_cicd:
        build_nixpacks_for_cicd(target_dir, new_image_tag, app_name)
    # 2. Đóng gói Docker Image bằng hàm chuyên trách nội bộ CI/CD
    write_log(app_name, f"   [2] Đang đóng gói Docker Image phiên bản mới: {new_image_tag}...")
    build_nixpacks_for_cicd(target_dir, new_image_tag, app_name)

    # 3. Đẩy Image lên Docker Hub
    write_log(app_name, f"   [3] Đang đẩy Image lên Docker Hub...")
    run_cmd(["docker", "push", new_image_tag], app_name)

    # 4. Cập nhật Deployment trên K3s (Rolling Update)
    write_log(app_name, f"   [4] Cập nhật phiên bản Deployment '{deploy_name}' trên K3s...")
    try:
        run_cmd(["kubectl", "set", "image", f"deployment/{deploy_name}", f"{deploy_name}={new_image_tag}", "-n", K8S_NAMESPACE], app_name)
    except Exception:
        try:
            c_name = subprocess.check_output(
                f"kubectl get deployment {deploy_name} -n {K8S_NAMESPACE} -o jsonpath='{{.spec.template.spec.containers[0].name}}'",
                shell=True, text=True
            ).strip()
            run_cmd(["kubectl", "set", "image", f"deployment/{deploy_name}", f"{c_name}={new_image_tag}", "-n", K8S_NAMESPACE], app_name)
        except Exception:
            run_cmd(["kubectl", "rollout", "restart", f"deployment/{deploy_name}", "-n", K8S_NAMESPACE], app_name)

    write_log(app_name, f"✅ [CI/CD - {component_name.upper()}] Triển khai thành công bản dựng: {commit_hash} từ kho {clean_display_url}!")


def redeploy_app(
    app_name: str,
    branch: str = "main",
    triggered_repo_url: Optional[str] = None,
    pull_git: bool = True,
    target_component: Optional[str] = None
):
    """
    Quy trình CI/CD ưu tiên tuyệt đối mã nguồn từ tài khoản kết nối qua Token:
    - Tự động nhận diện tài khoản GitHub sở hữu token từ Remote Origin hiện tại hoặc apps_config.json.
    - Chặn các Webhook từ repo của tác giả cũ để bảo vệ mã nguồn cá nhân hóa của người dùng.
    """
    comp_label = f" | Component: {target_component.upper()}" if target_component else ""
    write_log(app_name, f"\n=== [BẮT ĐẦU CI/CD REDEPLOY] {app_name} (Nhánh: {branch}{comp_label} | Pull Git: {pull_git}) ===")

    ws_be = os.path.join(BASE_WORKSPACE, f"{app_name}-be")
    ws_fe = os.path.join(BASE_WORKSPACE, f"{app_name}-fe")
    ws_mono = os.path.join(BASE_WORKSPACE, f"{app_name}-monorepo")
    ws_single = os.path.join(BASE_WORKSPACE, app_name)

    triggered_clean = get_clean_git_url(triggered_repo_url) if triggered_repo_url else None
    norm_comp = (target_component or "").lower().strip()
    app_cfg = get_active_app_config(app_name)

    # 1. NHẬN DIỆN CHỦ TÀI KHOẢN ĐANG KẾT NỐI (TỪ CONFIG HOẶC TRỰC TIẾP TỪ GIT ORIGIN HIỆN TẠI)
    active_token_owner = (app_cfg.get("primary_github_owner") or "").lower()
    if not active_token_owner:
        # Nếu chưa có trong config, tự động trích xuất từ remote origin của workspace thực tế
        for check_ws in [ws_be, ws_fe, ws_single, ws_mono]:
            if os.path.exists(check_ws):
                r_url = get_repo_remote_url(check_ws) or ""
                detected = extract_owner_from_git_url(r_url)
                if detected:
                    active_token_owner = detected
                    break

    # 2. BẢO VỆ MÃ NGUỒN: Chặn Webhook từ tác giả cũ nếu người dùng đã gắn repo tài khoản cá nhân
    if triggered_clean and active_token_owner:
        triggered_owner = extract_owner_from_git_url(triggered_clean) or ""
        if triggered_owner and triggered_owner != active_token_owner:
            write_log(
                app_name,
                f"⚠️ [SECURITY IGNORE] Bỏ qua Webhook từ tác giả gốc ({triggered_owner}) "
                f"vì ứng dụng đang được liên kết và ưu tiên theo tài khoản của token: [{active_token_owner}]."
            )
            write_log(app_name, "DONE")
            return

    # TRƯỜNG HỢP 1: FULLSTACK 2-REPO
    if os.path.exists(ws_be) or os.path.exists(ws_fe):
        write_log(app_name, f"🔍 Phát hiện cấu trúc Fullstack 2 Repositories riêng biệt.")
        rebuilt_any = False

        # Build Backend
        if os.path.exists(ws_be) and norm_comp not in ["fe", "frontend", "client", "web"]:
            be_origin = get_clean_git_url(get_repo_remote_url(ws_be) or "")
            should_rebuild_be = (
                norm_comp in ["be", "backend", "server", "api"]
                or (not triggered_clean)
                or (be_origin and be_origin in triggered_clean)
                or (triggered_clean in be_origin)
            )

            if should_rebuild_be:
                update_and_rebuild_component(
                    app_name=app_name,
                    component_name="backend",
                    target_dir=ws_be,
                    branch=branch,
                    deploy_name=f"{app_name}-be",
                    image_base=f"{DOCKER_USER}/{app_name}-be",
                    pull_git=pull_git
                )
                rebuilt_any = True

        # Build Frontend
        if os.path.exists(ws_fe) and norm_comp not in ["be", "backend", "server", "api"]:
            fe_origin = get_clean_git_url(get_repo_remote_url(ws_fe) or "")
            should_rebuild_fe = (
                norm_comp in ["fe", "frontend", "client", "web"]
                or (not triggered_clean)
                or (fe_origin and fe_origin in triggered_clean)
                or (triggered_clean in fe_origin)
            )

            if should_rebuild_fe:
                update_and_rebuild_component(
                    app_name=app_name,
                    component_name="frontend",
                    target_dir=ws_fe,
                    branch=branch,
                    deploy_name=f"{app_name}-fe",
                    image_base=f"{DOCKER_USER}/{app_name}-fe",
                    pull_git=pull_git
                )
                rebuilt_any = True

        if not rebuilt_any:
            write_log(app_name, f"ℹ️ Tiến hành cập nhật toàn bộ các component.")
            if os.path.exists(ws_be):
                update_and_rebuild_component(app_name, "backend", ws_be, branch, f"{app_name}-be", f"{DOCKER_USER}/{app_name}-be", pull_git)
            if os.path.exists(ws_fe):
                update_and_rebuild_component(app_name, "frontend", ws_fe, branch, f"{app_name}-fe", f"{DOCKER_USER}/{app_name}-fe", pull_git)

        write_log(app_name, f"=== [HOÀN TẤT CI/CD FULLSTACK] {app_name} ===")
        write_log(app_name, "DONE")
        return

    # TRƯỜNG HỢP 2: MONOREPO
    elif os.path.exists(ws_mono):
        write_log(app_name, f"🔍 Phát hiện cấu trúc Monorepo ({ws_mono}).")
        if pull_git:
            run_cmd(["git", "fetch", "--all"], app_name, cwd=ws_mono)
            try:
                run_cmd(["git", "reset", "--hard", f"origin/{branch}"], app_name, cwd=ws_mono)
            except Exception:
                run_cmd(["git", "reset", "--hard", "origin/main"], app_name, cwd=ws_mono)

        rebuilt_sub = False
        for candidate in ["backend", "server", "api", "be"]:
            sub_be = os.path.join(ws_mono, candidate)
            if os.path.exists(sub_be) and os.path.isdir(sub_be) and norm_comp not in ["fe", "frontend"]:
                update_and_rebuild_component(app_name, "backend", sub_be, branch, f"{app_name}-be", f"{DOCKER_USER}/{app_name}-be", pull_git)
                rebuilt_sub = True
                break

        for candidate in ["frontend", "client", "web", "fe"]:
            sub_fe = os.path.join(ws_mono, candidate)
            if os.path.exists(sub_fe) and os.path.isdir(sub_fe) and norm_comp not in ["be", "backend"]:
                update_and_rebuild_component(app_name, "frontend", sub_fe, branch, f"{app_name}-fe", f"{DOCKER_USER}/{app_name}-fe", pull_git)
                rebuilt_sub = True
                break

        if not rebuilt_sub:
            update_and_rebuild_component(app_name, "monorepo-root", ws_mono, branch, app_name, f"{DOCKER_USER}/{app_name}", pull_git)

        write_log(app_name, f"=== [HOÀN TẤT CI/CD MONOREPO] {app_name} ===")
        write_log(app_name, "DONE")
        return

    # TRƯỜNG HỢP 3: SINGLE REPO
    elif os.path.exists(ws_single):
        write_log(app_name, f"🔍 Phát hiện cấu trúc Single Repo ({ws_single}).")
        update_and_rebuild_component(
            app_name=app_name,
            component_name="app",
            target_dir=ws_single,
            branch=branch,
            deploy_name=app_name,
            image_base=f"{DOCKER_USER}/{app_name}",
            pull_git=pull_git
        )
        write_log(app_name, f"=== [HOÀN TẤT CI/CD SINGLE REPO] {app_name} ===")
        write_log(app_name, "DONE")
        return

    else:
        err_msg = f"❌ [LỖI CI/CD] Không tìm thấy thư mục workspace cho '{app_name}'."
        write_log(app_name, err_msg)
        write_log(app_name, "ERROR")
        raise FileNotFoundError(err_msg)
