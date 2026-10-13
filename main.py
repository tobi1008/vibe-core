import os
import json
import shutil
import subprocess
import re
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, Request, HTTPException, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from builder_core import deploy_app, delete_app, BASE_WORKSPACE, K8S_NAMESPACE
from archive_builder_core import deploy_archive_app, deploy_archive_with_db, UPLOAD_DIR
from cicd_core import redeploy_app
from db_builder_core import deploy_app_with_db, deploy_fullstack_app, delete_app_db_resources
from monorepo_builder_core import deploy_monorepo_app
try:
    from cms_builder_core import deploy_cms_app, delete_cms_db_resources
except Exception as e:
    deploy_cms_app = None
    delete_cms_db_resources = None
import requests

# Nạp router File Manager & Git an toàn
try:
    try:
        from file_manager_api import router as file_manager_router
    except ImportError:
        from file_manager_router import router as file_manager_router
except Exception as e:
    file_manager_router = None

# Nạp router Hermes Agent AI Debug an toàn (Bắt tất cả lỗi để không kéo sập API)
try:
    from hermes_agent_router import router as hermes_agent_router
except Exception as e:
    hermes_agent_router = None

app = FastAPI(title="Vibe Hosting API", version="2.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vibe.quyenlt.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_DB_FILE = "/opt/vibe-hosting/apps_config.json"
BACKUP_DIR = "/opt/vibe-hosting/backups"
LOG_DIR = "/opt/vibe-hosting/logs"
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

if file_manager_router:
    app.include_router(file_manager_router)

if hermes_agent_router:
    app.include_router(hermes_agent_router)

@app.get("/api/ai-providers")
def get_ai_providers():
    """Trả về danh sách AI Engines được hỗ trợ trên hệ thống (Antigravity & Hermes)"""
    from builder_core import AGY_MODELS, HERMES_MODELS
    return {
        "status": "success",
        "default": "agy",
        "providers": [
            {
                "id": "agy",
                "name": "Google Antigravity (Gemini Flash)",
                "badge": "Khuyên dùng - Nhanh (5-15s)",
                "description": "Thực thi CLI trực tiếp trên VPS, tự động dự phòng 5 tầng Gemini Flash",
                "models": AGY_MODELS
            },
            {
                "id": "hermes",
                "name": "Hermes Agent (Open-Source LLMs)",
                "badge": "Mô hình mã nguồn mở",
                "description": "Bộ não suy luận Hermes nguyên bản gọi qua API Gateway nội bộ",
                "models": HERMES_MODELS
            }
        ]
    }

@app.get("/api/open-source-templates")
def get_open_source_templates():
    """Trả về danh mục CMS và các ứng dụng mã nguồn mở hỗ trợ cài đặt Clean Database 1-click"""
    return {
        "status": "success",
        "templates": [
            {
                "id": "wordpress",
                "name": "WordPress (Official)",
                "category": "CMS & Blog",
                "default_repo": "https://github.com/WordPress/WordPress.git",
                "default_db": "mysql",
                "description": "Cài đặt WordPress nguyên bản từ GitHub chính thức, tự sinh 8 Salt keys, kích hoạt SSL Traefik reverse proxy và cấp phát MySQL trắng sạch.",
                "badge": "1-Click Setup"
            },
            {
                "id": "laravel",
                "name": "Laravel Starter",
                "category": "PHP Framework",
                "default_repo": "https://github.com/laravel/laravel.git",
                "default_db": "mysql",
                "description": "Khởi tạo Laravel sạch, tự động sao chép .env.example và tiêm cấu hình DB_HOST, DB_DATABASE, DB_USERNAME, APP_URL.",
                "badge": "Auto .env"
            },
            {
                "id": "ghost",
                "name": "Ghost CMS",
                "category": "Publishing Platform",
                "default_repo": "https://github.com/TryGhost/Ghost.git",
                "default_db": "mysql",
                "description": "Nền tảng blog xuất bản hiện đại viết bằng Node.js với MySQL.",
                "badge": "Node.js"
            },
            {
                "id": "custom",
                "name": "Custom Open-Source Repo",
                "category": "Tùy biến GitHub",
                "default_repo": "",
                "default_db": "mysql",
                "description": "Triển khai bất kỳ repository GitHub mã nguồn mở nào với Database trắng hoàn toàn (MySQL, PostgreSQL, MongoDB, MariaDB) không cần file backup.",
                "badge": "Clean DB"
            }
        ]
    }

def get_app_config(app_name: str) -> dict:
    if not os.path.exists(CONFIG_DB_FILE):
        return {"auto_cicd": True}
    with open(CONFIG_DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get(app_name, {"auto_cicd": True})

def save_app_config(app_name: str, config: dict):
    data = {}
    if os.path.exists(CONFIG_DB_FILE):
        with open(CONFIG_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    current = data.get(app_name, {})
    current.update(config)
    data[app_name] = current
    with open(CONFIG_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def resolve_github_owner_from_token(token: Optional[str]) -> Optional[str]:
    """Tự động xác thực Token với GitHub để lấy username chính chủ"""
    if not token or not token.strip():
        return None
    try:
        res = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token.strip()}"},
            timeout=8
        )
        if res.status_code == 200:
            return res.json().get("login")
    except Exception:
        pass
    return None

def prepare_repo_url(url: str, token: Optional[str]) -> str:
    if token and "github.com" in url and "@" not in url:
        return url.replace("https://", f"https://{token}@")
    return url

class DeployRequest(BaseModel):
    repo_url: str
    app_name: str
    app_domain: str
    github_token: Optional[str] = None
    ai_provider: Optional[str] = "agy"

class AppConfigUpdate(BaseModel):
    auto_cicd: bool

class SyncRepoRequest(BaseModel):
    github_token: Optional[str] = None
    be_repo_url: Optional[str] = None
    fe_repo_url: Optional[str] = None
    repo_url: Optional[str] = None

class RedeployRequest(BaseModel):
    branch: Optional[str] = "main"
    target_component: Optional[str] = None
    pull_git: Optional[bool] = True
    triggered_repo_url: Optional[str] = None

class MonorepoDeployRequest(BaseModel):
    repo_url: str
    app_name: str
    be_domain: str
    fe_domain: Optional[str] = None
    db_type: Optional[str] = "mysql"
    github_token: Optional[str] = None
    ai_provider: Optional[str] = "agy"

class OpenSourceDeployRequest(BaseModel):
    repo_url: Optional[str] = "wordpress"
    app_name: str
    app_domain: str
    db_type: Optional[str] = "mysql"
    github_token: Optional[str] = None
    ai_provider: Optional[str] = "agy"
    cms_type: Optional[str] = "wordpress"

class RollbackRequest(BaseModel):
    component: Optional[str] = None  # "be", "fe", hoặc None (cho app single repo)
    revision: Optional[int] = 0      # Truyền 0 để tự động lùi về 1 phiên bản liền trước

@app.post("/api/deploy")
async def trigger_deploy(req: DeployRequest, background_tasks: BackgroundTasks):
    final_url = prepare_repo_url(req.repo_url, req.github_token)
    owner = resolve_github_owner_from_token(req.github_token)
    ai_engine = (req.ai_provider or "agy").lower().strip()

    with open(f"{LOG_DIR}/{req.app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"🚀 [INIT] Bắt đầu quy trình Deploy Web: {req.app_name} ({req.app_domain}) - AI: {ai_engine.upper()}...\n")
    
    save_app_config(req.app_name, {
        "auto_cicd": True,
        "repo_url": req.repo_url,
        "primary_github_owner": owner,
        "has_token": bool(req.github_token),
        "ai_provider": ai_engine
    })
    background_tasks.add_task(deploy_app, final_url, req.app_name, req.app_domain, ai_engine)
    return {"status": "success", "message": f"Hệ thống đang tiến hành Deploy {req.app_name} (AI: {ai_engine.upper()})."}

@app.post("/api/deploy-open-source")
async def trigger_deploy_open_source(req: OpenSourceDeployRequest, background_tasks: BackgroundTasks):
    """
    API Triển khai CMS (WordPress mặc định) hoặc bất kỳ repo mã nguồn mở nào với Clean Database (DB Trắng)
    Không yêu cầu người dùng nạp file backup SQL.
    """
    target_repo = (req.repo_url or "").strip()
    cms_type = (req.cms_type or "").strip().lower()

    if not target_repo or target_repo.lower() in ["wordpress", "wp", "default"] or cms_type == "wordpress":
        final_url = "https://github.com/WordPress/WordPress.git"
        detected_cms = "wordpress"
    else:
        final_url = prepare_repo_url(target_repo, req.github_token)
        detected_cms = cms_type or "custom"

    owner = resolve_github_owner_from_token(req.github_token)
    ai_engine = (req.ai_provider or "agy").lower().strip()
    db_type = (req.db_type or "mysql").lower().strip()

    with open(f"{LOG_DIR}/{req.app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"🚀 [INIT] Bắt đầu quy trình Deploy CMS/Open-Source ({detected_cms.upper()}): {req.app_name} ({req.app_domain}) - Clean DB ({db_type.upper()}) - AI: {ai_engine.upper()}...\n")

    save_app_config(req.app_name, {
        "auto_cicd": True,
        "deploy_type": "open_source_clean_db",
        "cms_type": detected_cms,
        "repo_url": final_url,
        "db_type": db_type,
        "primary_github_owner": owner,
        "has_token": bool(req.github_token),
        "ai_provider": ai_engine
    })

    if not deploy_cms_app:
        return {"status": "error", "message": "CMS Builder module chưa sẵn sàng hoặc bị thiếu trên máy chủ."}

    background_tasks.add_task(
        deploy_cms_app,
        final_url,
        req.app_name,
        req.app_domain,
        db_type,
        ai_engine,
        detected_cms
    )

    return {
        "status": "success",
        "message": f"Hệ thống đang tiến hành triển khai CMS/Mã nguồn mở {req.app_name} với Clean Database ({db_type.upper()}) (AI: {ai_engine.upper()})."
    }

@app.post("/api/deploy-with-db")
async def trigger_deploy_db(
    background_tasks: BackgroundTasks,
    repo_url: str = Form(...),
    app_name: str = Form(...),
    app_domain: str = Form(...),
    db_type: str = Form(...),
    github_token: Optional[str] = Form(None),
    backup_file: Optional[UploadFile] = File(None),
    ai_provider: Optional[str] = Form("agy")
):
    final_url = prepare_repo_url(repo_url, github_token)
    owner = resolve_github_owner_from_token(github_token)
    ai_engine = (ai_provider or "agy").lower().strip()
    saved_backup_path = ""
    if backup_file and backup_file.filename:
        safe_filename = os.path.basename(backup_file.filename)
        saved_backup_path = f"{BACKUP_DIR}/{app_name}_{safe_filename}"
        try:
            with open(saved_backup_path, "wb") as buffer:
                shutil.copyfileobj(backup_file.file, buffer)
        except Exception as e:
            return {"status": "error", "message": f"Lỗi lưu file DB: {str(e)}"}
        finally:
            backup_file.file.close()

    with open(f"{LOG_DIR}/{app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"🚀 [INIT] Bắt đầu quy trình Deploy có Database: {app_name} ({db_type.upper()}) - AI: {ai_engine.upper()}...\n")

    save_app_config(app_name, {
        "auto_cicd": True,
        "repo_url": repo_url,
        "db_type": db_type,
        "primary_github_owner": owner,
        "has_token": bool(github_token),
        "ai_provider": ai_engine
    })
    background_tasks.add_task(deploy_app_with_db, final_url, app_name, app_domain, db_type, saved_backup_path, ai_engine)
    return {"status": "success", "message": f"Đã tiếp nhận yêu cầu cấp phát {db_type.upper()} cho {app_name} (AI: {ai_engine.upper()})."}

@app.post("/api/deploy-archive")
async def trigger_deploy_archive(
    background_tasks: BackgroundTasks,
    archive_file: UploadFile = File(...),
    app_name: str = Form(...),
    app_domain: str = Form(...),
    ai_provider: Optional[str] = Form("agy")
):
    safe_filename = os.path.basename(archive_file.filename)
    saved_archive_path = f"{UPLOAD_DIR}/{app_name}_{safe_filename}"
    ai_engine = (ai_provider or "agy").lower().strip()
    try:
        with open(saved_archive_path, "wb") as buffer:
            shutil.copyfileobj(archive_file.file, buffer)
    except Exception as e:
        return {"status": "error", "message": f"Lỗi lưu file nén tải lên: {str(e)}"}
    finally:
        archive_file.file.close()

    with open(f"{LOG_DIR}/{app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"🚀 [INIT] Bắt đầu quy trình Deploy từ file nén: {app_name} ({safe_filename}) - AI: {ai_engine.upper()}...\n")

    save_app_config(app_name, {
        "auto_cicd": False,
        "deploy_type": "archive",
        "archive_filename": safe_filename,
        "app_domain": app_domain,
        "ai_provider": ai_engine
    })

    background_tasks.add_task(deploy_archive_app, saved_archive_path, app_name, app_domain, ai_engine)
    return {
        "status": "success",
        "message": f"Hệ thống đã nhận file nén [{safe_filename}] và đang tiến hành giải nén & triển khai {app_name} (AI: {ai_engine.upper()})."
    }

@app.post("/api/deploy-archive-with-db")
async def trigger_deploy_archive_with_db(
    background_tasks: BackgroundTasks,
    archive_file: UploadFile = File(...),
    app_name: str = Form(...),
    app_domain: str = Form(...),
    db_type: str = Form(...),
    backup_file: Optional[UploadFile] = File(None),
    ai_provider: Optional[str] = Form("agy")
):
    safe_filename = os.path.basename(archive_file.filename)
    saved_archive_path = f"{UPLOAD_DIR}/{app_name}_{safe_filename}"
    ai_engine = (ai_provider or "agy").lower().strip()
    try:
        with open(saved_archive_path, "wb") as buffer:
            shutil.copyfileobj(archive_file.file, buffer)
    except Exception as e:
        return {"status": "error", "message": f"Lỗi lưu file nén tải lên: {str(e)}"}
    finally:
        archive_file.file.close()

    saved_backup_path = ""
    if backup_file and backup_file.filename:
        safe_backup_name = os.path.basename(backup_file.filename)
        saved_backup_path = f"{BACKUP_DIR}/{app_name}_{safe_backup_name}"
        try:
            with open(saved_backup_path, "wb") as buffer:
                shutil.copyfileobj(backup_file.file, buffer)
        except Exception as e:
            return {"status": "error", "message": f"Lỗi lưu file DB backup: {str(e)}"}
        finally:
            backup_file.file.close()

    with open(f"{LOG_DIR}/{app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"🚀 [INIT] Bắt đầu quy trình Deploy file nén có Database: {app_name} ({db_type.upper()}) - AI: {ai_engine.upper()}...\n")

    save_app_config(app_name, {
        "auto_cicd": False,
        "deploy_type": "archive_with_db",
        "db_type": db_type,
        "archive_filename": safe_filename,
        "app_domain": app_domain,
        "ai_provider": ai_engine
    })

    background_tasks.add_task(
        deploy_archive_with_db,
        saved_archive_path,
        app_name,
        app_domain,
        db_type,
        saved_backup_path,
        ai_engine
    )
    return {
        "status": "success",
        "message": f"Đã tiếp nhận file nén và yêu cầu cấp phát {db_type.upper()} cho {app_name} (AI: {ai_engine.upper()})."
    }

@app.post("/api/deploy-fullstack")
async def trigger_deploy_fullstack(
    background_tasks: BackgroundTasks,
    app_name: str = Form(...),
    be_repo_url: str = Form(...),
    be_domain: str = Form(...),
    fe_repo_url: str = Form(...),
    fe_domain: str = Form(...),
    db_type: str = Form(...),
    github_token: Optional[str] = Form(None),
    backup_file: Optional[UploadFile] = File(None),
    ai_provider: Optional[str] = Form("agy")
):
    final_be_url = prepare_repo_url(be_repo_url, github_token)
    final_fe_url = prepare_repo_url(fe_repo_url, github_token)
    owner = resolve_github_owner_from_token(github_token)
    ai_engine = (ai_provider or "agy").lower().strip()

    saved_backup_path = ""
    if backup_file and backup_file.filename:
        safe_filename = os.path.basename(backup_file.filename)
        saved_backup_path = f"{BACKUP_DIR}/{app_name}_{safe_filename}"
        try:
            with open(saved_backup_path, "wb") as buffer:
                shutil.copyfileobj(backup_file.file, buffer)
        except Exception as e:
            return {"status": "error", "message": f"Lỗi lưu file backup: {str(e)}"}
        finally:
            backup_file.file.close()

    with open(f"{LOG_DIR}/{app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"🚀 [INIT] Đã tiếp nhận yêu cầu triển khai Fullstack (2 Repos): {app_name} - AI: {ai_engine.upper()}\n"
                f"   - Frontend: {fe_domain} ({fe_repo_url})\n"
                f"   - Backend : {be_domain} ({be_repo_url})\n"
                f"   - Database: {db_type.upper()}\n"
                f"---------------------------------------------------\n")

    save_app_config(app_name, {
        "auto_cicd": True,
        "be_repo_url": be_repo_url,
        "fe_repo_url": fe_repo_url,
        "be_domain": be_domain,
        "fe_domain": fe_domain,
        "db_type": db_type,
        "primary_github_owner": owner,
        "has_token": bool(github_token),
        "ai_provider": ai_engine
    })
    background_tasks.add_task(
        deploy_fullstack_app,
        app_name, final_be_url, be_domain, final_fe_url, fe_domain, db_type, saved_backup_path, ai_engine
    )
    return {
        "status": "success",
        "message": f"Bắt đầu khởi chạy cụm Fullstack {app_name} (FE: {fe_domain} | BE: {be_domain})."
    }

@app.post("/api/deploy-monorepo")
async def trigger_deploy_monorepo(req: MonorepoDeployRequest, background_tasks: BackgroundTasks):
    final_url = prepare_repo_url(req.repo_url, req.github_token)
    owner = resolve_github_owner_from_token(req.github_token)

    ai_engine = (req.ai_provider or "agy").lower().strip()
    with open(f"{LOG_DIR}/{req.app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"🚀 [INIT] Tiếp nhận Monorepo Open Source: {req.app_name} (AI: {ai_engine.upper()})\n"
                f"   - Repository : {req.repo_url}\n"
                f"   - Backend API: {req.be_domain}\n"
                f"   - Frontend UI: {req.fe_domain if req.fe_domain else 'None'}\n"
                f"   - Database   : {req.db_type.upper()} (Auto-Schema / Empty)\n"
                f"   - AI Engine  : {ai_engine.upper()}\n"
                f"---------------------------------------------------\n")

    save_app_config(req.app_name, {
        "auto_cicd": True,
        "repo_url": req.repo_url,
        "primary_github_owner": owner,
        "has_token": bool(req.github_token),
        "ai_provider": ai_engine
    })
    background_tasks.add_task(
        deploy_monorepo_app,
        final_url, req.app_name, req.be_domain, req.fe_domain or "", req.db_type, ai_engine
    )
    return {
        "status": "success",
        "message": f"Đã tiếp nhận Monorepo {req.app_name} (AI: {ai_engine.upper()}). Hệ thống đang tự động nhận diện và triển khai."
    }

@app.post("/api/apps/{app_name}/sync-repo")
async def sync_app_repo(app_name: str, req: SyncRepoRequest):
    """API giúp giao diện Web đồng bộ hoặc đổi link repo mới bất kỳ lúc nào mà không cần chạm VPS"""
    owner = resolve_github_owner_from_token(req.github_token)
    updates = {}
    if owner:
        updates["primary_github_owner"] = owner
    if req.be_repo_url:
        updates["be_repo_url"] = req.be_repo_url
    if req.fe_repo_url:
        updates["fe_repo_url"] = req.fe_repo_url
    if req.repo_url:
        updates["repo_url"] = req.repo_url

    save_app_config(app_name, updates)
    return {
        "status": "success",
        "message": f"Đã cập nhật cấu hình mã nguồn cho {app_name}",
        "config": get_app_config(app_name)
    }

def purge_app_workspace_and_configs(app_name: str):
    """
    Dọn dẹp triệt để 100% các biến thể thư mục Workspace, file cấu hình và logs:
    - Hỗ trợ Fullstack 2-repo: {app_name}-be, {app_name}-fe
    - Hỗ trợ Monorepo: {app_name}-monorepo
    - Hỗ trợ Single repo: {app_name}
    - Dọn dẹp apps_config.json và file log
    """
    # 1. Dọn dẹp tài nguyên Kubernetes và Database
    try:
        delete_cms_db_resources(app_name)
    except Exception:
        pass

    try:
        delete_app_db_resources(app_name)
    except Exception:
        pass

    try:
        delete_app(app_name)
    except Exception:
        pass

    # 2. Xóa sạch mọi thư mục Workspace liên quan
    candidate_dirs = [
        os.path.join(BASE_WORKSPACE, app_name),
        os.path.join(BASE_WORKSPACE, f"{app_name}-be"),
        os.path.join(BASE_WORKSPACE, f"{app_name}-fe"),
        os.path.join(BASE_WORKSPACE, f"{app_name}-monorepo"),
    ]

    for target_ws in candidate_dirs:
        if os.path.exists(target_ws):
            try:
                shutil.rmtree(target_ws, ignore_errors=True)
            except Exception:
                pass
            if os.path.exists(target_ws):
                subprocess.run(["rm", "-rf", target_ws], check=False)

    # 3. Xóa cấu hình của app trong apps_config.json
    try:
        if os.path.exists(CONFIG_DB_FILE):
            with open(CONFIG_DB_FILE, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            if app_name in cfg_data:
                del cfg_data[app_name]
                with open(CONFIG_DB_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

    # 4. Xóa log build cũ để chuẩn bị môi trường sạch
    try:
        log_path = f"{LOG_DIR}/{app_name}.log"
        if os.path.exists(log_path):
            os.remove(log_path)
    except Exception:
        pass

@app.delete("/api/apps/{app_name}")
async def remove_app(app_name: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(purge_app_workspace_and_configs, app_name)
    return {
        "status": "success",
        "message": f"Hệ thống đang tiến hành dọn dẹp sạch toàn bộ tài nguyên (BE, FE, Database, Workspaces) của {app_name}."
    }

@app.get("/api/logs/{app_name}")
async def get_build_logs(app_name: str):
    log_path = f"{LOG_DIR}/{app_name}.log"
    if not os.path.exists(log_path):
        return {
            "status": "waiting",
            "logs": f"⏳ [INIT] Hệ thống đang chuẩn bị môi trường cho {app_name}..."
        }
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {
            "status": "success",
            "logs": content if content.strip() else f"⏳ Đang khởi tạo tiến trình cho {app_name}..."
        }
    except Exception as e:
        return {"status": "waiting", "logs": f"⏳ Đang cập nhật log... ({str(e)})"}

@app.get("/api/pod-logs/{app_name}")
def get_pod_runtime_logs(app_name: str, component: Optional[str] = Query(None), lines: int = Query(200, ge=10, le=2000)):
    try:
        target_name = f"{app_name}-{component}" if component in ["be", "fe"] else app_name
        check_cmd = f"kubectl get pods -n {K8S_NAMESPACE} -l 'app in ({target_name}, {app_name})' -o jsonpath='{{.items[*].metadata.name}}'"
        pod_names = subprocess.check_output(check_cmd, shell=True, text=True).strip()

        if not pod_names:
            return {"status": "warning", "logs": "Chưa tìm thấy Pod đang chạy cho ứng dụng này."}

        target_pod = pod_names.split()[0]
        result = subprocess.run(["kubectl", "logs", target_pod, "-n", K8S_NAMESPACE, f"--tail={lines}"], capture_output=True, text=True)
        pod_output = result.stdout

        if not pod_output and result.stderr:
            prev_result = subprocess.run(["kubectl", "logs", target_pod, "-n", K8S_NAMESPACE, "--previous", f"--tail={lines}"], capture_output=True, text=True)
            pod_output = f"[LOG TỪ LẦN CRASH TRƯỚC ĐÓ]:\n{prev_result.stdout}" if prev_result.stdout else result.stderr

        return {
            "status": "success",
            "pod_name": target_pod,
            "logs": pod_output if pod_output else "Container đang chạy nhưng chưa sinh thêm log mới."
        }
    except Exception as e:
        return {"status": "error", "message": f"Không thể lấy log: {str(e)}"}

@app.get("/api/apps/{app_name}/config")
async def read_config(app_name: str):
    return {"status": "success", "data": get_app_config(app_name)}

@app.post("/api/apps/{app_name}/config")
async def update_config(app_name: str, req: AppConfigUpdate):
    save_app_config(app_name, {"auto_cicd": req.auto_cicd})
    return {"status": "success", "message": f"Đã {'bật' if req.auto_cicd else 'tắt'} Auto CI/CD cho {app_name}"}

@app.post("/api/apps/{app_name}/redeploy")
async def manual_redeploy(
    app_name: str,
    background_tasks: BackgroundTasks,
    req: Optional[RedeployRequest] = None
):
    branch = req.branch if req and req.branch else "main"
    target_comp = req.target_component if req else None
    pull_git = req.pull_git if req and req.pull_git is not None else True
    triggered_url = req.triggered_repo_url if req else None

    background_tasks.add_task(
        redeploy_app,
        app_name=app_name,
        branch=branch,
        triggered_repo_url=triggered_url,
        pull_git=pull_git,
        target_component=target_comp
    )
    return {"status": "success", "message": f"Đã kích hoạt Rebuild cho {app_name} (Nhánh: {branch})."}

@app.post("/api/webhooks/github/{app_name}")
async def github_webhook(app_name: str, request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    app_config = get_app_config(app_name)
    if not app_config.get("auto_cicd", True):
        return {"status": "ignored", "message": "Auto CI/CD đang tắt."}

    if "commits" in payload or "ref" in payload:
        branch = payload.get("ref", "refs/heads/main").split("/")[-1]
        
        # Trích xuất URL của kho lưu trữ vừa push để CI/CD nhận diện đúng component cần build
        repo_info = payload.get("repository", {})
        triggered_repo_url = repo_info.get("clone_url") or repo_info.get("html_url") or ""

        background_tasks.add_task(redeploy_app, app_name, branch, triggered_repo_url)
        return {"status": "success", "message": f"CI/CD Pipeline đã kích hoạt cho [{app_name}]"}

    return {"status": "ignored", "message": "Không có push code mới."}

@app.get("/api/apps/{app_name}/history")
def get_app_rollout_history(app_name: str, component: Optional[str] = Query(None)):
    """API lấy lịch sử các phiên bản Deploy của K8s"""
    target_deploy = f"{app_name}-{component}" if component in ["be", "fe"] else app_name
    
    try:
        # Kiểm tra Deployment có tồn tại không
        check_cmd = f"kubectl get deployment {target_deploy} -n {K8S_NAMESPACE}"
        subprocess.run(check_cmd, shell=True, check=True, capture_output=True)

        # Lấy lịch sử từ K8s
        history_cmd = f"kubectl rollout history deployment/{target_deploy} -n {K8S_NAMESPACE}"
        result = subprocess.run(history_cmd, shell=True, capture_output=True, text=True, check=True)
        
        # Phân tích chuỗi text K8s trả về thành mảng JSON JSON
        lines = result.stdout.strip().split('\n')
        revisions = []
        for line in lines:
            # Tìm các dòng bắt đầu bằng số (số Revision)
            match = re.match(r'^\s*(\d+)\s+(.*)', line)
            if match:
                rev_num = int(match.group(1))
                cause = match.group(2).strip()
                if cause == "<none>":
                    cause = "Bản cập nhật tự động (CI/CD / Image Update)"
                revisions.append({"revision": rev_num, "cause": cause})

        # Sắp xếp để phiên bản mới nhất (số to nhất) lên đầu
        revisions.sort(key=lambda x: x["revision"], reverse=True)

        return {
            "status": "success",
            "app_name": app_name,
            "target": target_deploy,
            "history": revisions
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy Deployment '{target_deploy}' hoặc chưa có lịch sử. (Lỗi: {e.stderr})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/apps/{app_name}/rollback")
def trigger_app_rollback(app_name: str, req: RollbackRequest):
    """API Kích hoạt Rollback về phiên bản cũ"""
    target_deploy = f"{app_name}-{req.component}" if req.component in ["be", "fe"] else app_name
    
    try:
        # Lệnh rollback. Nếu revision = 0, K8s tự lùi về bản gần nhất.
        if req.revision and req.revision > 0:
            rollback_cmd = f"kubectl rollout undo deployment/{target_deploy} --to-revision={req.revision} -n {K8S_NAMESPACE}"
            msg = f"Đã khôi phục {target_deploy} về Phiên bản {req.revision}!"
        else:
            rollback_cmd = f"kubectl rollout undo deployment/{target_deploy} -n {K8S_NAMESPACE}"
            msg = f"Đã khôi phục {target_deploy} về phiên bản ổn định ngay trước đó!"

        # Chạy lệnh
        result = subprocess.run(rollback_cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            return {"status": "success", "message": msg, "output": result.stdout.strip()}
        else:
            return {"status": "error", "message": f"Rollback thất bại: {result.stderr.strip()}"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi hệ thống khi rollback: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
