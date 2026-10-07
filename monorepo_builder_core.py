import os
import subprocess
import time
import json
import re
import secrets

from builder_core import write_log, run_cmd, DOCKER_USER, BASE_WORKSPACE, K8S_NAMESPACE
from db_builder_core import (
    generate_db_password,
    get_db_strategy,
    generate_db_manifest,
    generate_standard_deployment,
    generate_standard_service,
    generate_standard_ingress,
    detect_app_port,
    auto_patch_nodejs_esm_conflicts,
    link_frontend_to_backend,
    analyze_and_fix_backend,
)


# 1. HÀM TẠO DATABASE_URL CHUẨN CHO PRISMA
def build_database_url(db_info: dict, host_override: str = None) -> str:
    """Tạo chuỗi connection string DATABASE_URL phù hợp với từng loại cơ sở dữ liệu"""
    db_type = db_info.get("type", "mysql").lower()
    user = db_info.get("user", "root")
    password = db_info.get("password", "")
    host = host_override or db_info.get("host", "localhost")
    port = db_info.get("port", 3306)
    db_name = db_info.get("db_name", "")

    if db_type in ["mysql", "mariadb"]:
        return f"mysql://{user}:{password}@{host}:{port}/{db_name}"
    elif db_type in ["postgresql", "postgres"]:
        return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
    elif db_type == "mongodb":
        return f"mongodb://{user}:{password}@{host}:{port}/{db_name}?authSource=admin"
    return ""


# 2. TỰ ĐỘNG BÙ CÁC BIẾN MÔI TRƯỜNG THIẾU (JWT_SECRET, CORS, DEMO SEED, .ENV.EXAMPLE...)
def extract_env_defaults(be_dir: str, app_name: str, fe_domain: str = "") -> dict:
    """Tự động sinh các biến Auth cốt lõi và quét toàn bộ template .env.example để tránh getOrThrow crash"""
    fe_clean = fe_domain.replace("https://", "").replace("http://", "").strip("/") if fe_domain else ""
    fe_url = f"https://{fe_clean}" if fe_clean else "*"
    cors_origins = f"{fe_url},http://{fe_clean},http://localhost:3000,*" if fe_clean else "*"

    defaults = {
        "ENABLE_DEMO_DATA": "true",
        "JWT_SECRET": secrets.token_hex(32),
        "JWT_EXPIRES_IN": "7d",
        "REFRESH_TOKEN_SECRET": secrets.token_hex(32),
        "CORS_ORIGIN": cors_origins,
        "CORS_ORIGINS": cors_origins,
        "ALLOWED_ORIGINS": cors_origins,
        "FRONTEND_URL": fe_url,
        "CLIENT_URL": fe_url,
        "WEB_URL": fe_url,
        "APP_URL": fe_url,
        "APP_NAME": app_name,
    }

    for ex_name in [".env.example", ".env.sample", ".env.local.example"]:
        ex_path = os.path.join(be_dir, ex_name)
        if os.path.exists(ex_path):
            try:
                with open(ex_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k not in defaults:
                            defaults[k] = v if v else f"default_{k.lower()}_val"
            except Exception:
                pass
    return defaults


# 3. GỠ BỎ TRIỆT ĐỂ BỘ CHẶN CORS CỨNG TRONG BACKEND (NGĂN LỖI 500 PREFLIGHT)
def patch_backend_cors(be_dir: str, fe_domain: str, app_name: str):
    """
    Quét mã nguồn Backend (src/main.ts, main.js) để thay thế logic callback CORS,
    chuyển thành callback(null, true) bất kể Origin nào gửi tới.
    """
    for root, dirs, files in os.walk(be_dir):
        if "node_modules" in dirs:
            dirs.remove("node_modules")
        if ".git" in dirs:
            dirs.remove(".git")
        for fname in files:
            if fname in ["main.ts", "main.js"]:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()

                    new_content = content
                    # Khớp chính xác mẫu callback(new Error(...), false)
                    new_content = re.sub(
                        r"callback\s*\(\s*new\s+Error\([^)]*\)\s*,\s*false\s*\);?",
                        "callback(null, true);",
                        new_content
                    )
                    # Khớp các mẫu ném lỗi CORS khác
                    new_content = re.sub(
                        r"callback\s*\(\s*new\s+Error\([^)]*CORS[^)]*\)\s*\);?",
                        "callback(null, true);",
                        new_content,
                        flags=re.IGNORECASE
                    )

                    # Ghi đè cấu hình enableCors chuẩn an toàn
                    cors_replacement = "app.enableCors({ origin: true, credentials: true, methods: 'GET,HEAD,PUT,PATCH,POST,DELETE,OPTIONS', allowedHeaders: 'Content-Type,Accept,Authorization,X-Requested-With' });"
                    new_content = re.sub(
                        r"app\.enableCors\s*\(\s*\{[\s\S]*?\}\s*\);",
                        cors_replacement,
                        new_content
                    )

                    if new_content != content:
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        write_log(app_name, f"   -> [BE CORS Patch] Đã mở toàn quyền CORS trong {os.path.relpath(fpath, be_dir)}")
                except Exception as e:
                    write_log(app_name, f"   -> [BE CORS Warning] Lỗi xử lý {fname}: {e}")


# 4. TẠO DEPLOYMENT MANIFEST CHO BACKEND
def generate_monorepo_be_deployment(app_name: str, image: str, port: int, db_info: dict, db_url: str, extra_envs: dict = None):
    """Sinh manifest deployment chứa đầy đủ DATABASE_URL, JWT, CORS, ENABLE_DEMO_DATA và cấu hình runtime"""
    env_vars = [
        {"name": "PORT", "value": str(port)},
        {"name": "NODE_ENV", "value": "production"},
        {"name": "NODE_OPTIONS", "value": "--experimental-require-module"},
        {"name": "DATABASE_URL", "value": db_url},
        {"name": "ENABLE_DEMO_DATA", "value": "true"},
    ]

    if db_info:
        env_vars.extend([
            {"name": "DB_HOST", "value": str(db_info.get("host", ""))},
            {"name": "DB_PORT", "value": str(db_info.get("port", ""))},
            {"name": "DB_USER", "value": str(db_info.get("user", ""))},
            {"name": "DB_PASSWORD", "value": str(db_info.get("password", ""))},
            {"name": "DB_NAME", "value": str(db_info.get("db_name", ""))},
        ])

    if extra_envs:
        for k, v in extra_envs.items():
            if not any(e["name"] == k for e in env_vars):
                env_vars.append({"name": k, "value": str(v)})

    env_str = "\n".join([f"        - name: {e['name']}\n          value: \"{e['value']}\"" for e in env_vars])

    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {app_name}
  namespace: {K8S_NAMESPACE}
  labels:
    app: {app_name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {app_name}
  template:
    metadata:
      labels:
        app: {app_name}
    spec:
      containers:
      - name: {app_name}
        image: {image}
        imagePullPolicy: Always
        ports:
        - containerPort: {port}
        env:
{env_str}
"""


# 5. TỰ ĐỘNG NHẬN DIỆN CẤU TRÚC THƯ MỤC MONOREPO
def detect_monorepo_structure(workspace_dir: str):
    """
    Quét tìm thư mục con chứa Backend và Frontend.
    Nếu không tìm thấy thư mục con, mặc định thư mục gốc là Backend.
    """
    be_candidates = ["backend", "server", "api", "be", "service", "core"]
    fe_candidates = ["frontend", "client", "web", "ui", "fe"]

    be_dir = None
    fe_dir = None

    items = [d for d in os.listdir(workspace_dir) if os.path.isdir(os.path.join(workspace_dir, d))]

    for item in items:
        lower = item.lower()
        if not be_dir and any(c == lower or lower.startswith(c) for c in be_candidates):
            be_dir = os.path.join(workspace_dir, item)
        elif not fe_dir and any(c == lower or lower.startswith(c) for c in fe_candidates):
            fe_dir = os.path.join(workspace_dir, item)

    if not be_dir or not fe_dir:
        for item in items:
            if item in [".git", "node_modules", "k8s", "dist", "build"]:
                continue
            pkg_path = os.path.join(workspace_dir, item, "package.json")
            if os.path.exists(pkg_path):
                try:
                    with open(pkg_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                    if any(k in deps for k in ["vite", "react", "vue", "next", "nuxt"]) and not fe_dir:
                        fe_dir = os.path.join(workspace_dir, item)
                    elif any(k in deps for k in ["express", "@nestjs/core", "koa", "fastify", "mongoose", "sequelize"]) and not be_dir:
                        be_dir = os.path.join(workspace_dir, item)
                except Exception:
                    pass

    be_dir = be_dir or workspace_dir
    return be_dir, fe_dir


# 6. TỐI ƯU HÓA CẤU HÌNH NEXT.JS (GỠ BỎ STANDALONE)
def fix_nextjs_standalone_config(fe_dir: str, app_name: str):
    """Gỡ bỏ output: 'standalone' trong next.config để next start hoạt động chuẩn xác"""
    for fname in ["next.config.js", "next.config.mjs", "next.config.ts"]:
        fpath = os.path.join(fe_dir, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                if "standalone" in content:
                    new_content = re.sub(r"output\s*:\s*['\"]standalone['\"]\s*,?", "", content)
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    write_log(app_name, f"   -> [Next.js Patch] Đã tắt 'output: standalone' trong {fname}.")
            except Exception as e:
                write_log(app_name, f"   -> [Next.js Warning] Bỏ qua sửa {fname}: {e}")


# 7. QUÉT VÀ THAY THẾ TOÀN BỘ LOCALHOST TRONG FRONTEND BẰNG DOMAIN THẬT
def patch_frontend_api_endpoints(fe_dir: str, be_domain: str, app_name: str):
    """
    Tìm và thay thế triệt để các chuỗi localhost:5000 / localhost:8080 bị hardcode trong source FE,
    đồng thời tạo file .env.production và .env.local chuẩn.
    """
    be_api_url = f"https://{be_domain}/api"
    be_root_url = f"https://{be_domain}"

    write_log(app_name, f"   -> [FE Patch] Đang cấu hình URL API Backend: {be_api_url}")

    env_content = f"""NEXT_PUBLIC_API_URL={be_api_url}
NEXT_PUBLIC_API_BASE_URL={be_api_url}
NEXT_PUBLIC_BACKEND_URL={be_root_url}
NEXT_PUBLIC_SERVER_URL={be_root_url}
API_URL={be_api_url}
"""
    for env_file in [".env", ".env.local", ".env.production"]:
        with open(os.path.join(fe_dir, env_file), "w", encoding="utf-8") as f:
            f.write(env_content)

    extensions = (".ts", ".tsx", ".js", ".jsx", ".json", ".mjs")
    patterns = [
        (re.compile(r"http://localhost:5000/api/?"), be_api_url),
        (re.compile(r"http://localhost:5000"), be_root_url),
        (re.compile(r"http://127\.0\.0\.1:5000/api/?"), be_api_url),
        (re.compile(r"http://127\.0\.0\.1:5000"), be_root_url),
        (re.compile(r"http://localhost:8080/api/?"), be_api_url),
        (re.compile(r"http://localhost:8080"), be_root_url),
    ]

    for root, dirs, files in os.walk(fe_dir):
        if "node_modules" in dirs:
            dirs.remove("node_modules")
        if ".next" in dirs:
            dirs.remove(".next")
        if ".git" in dirs:
            dirs.remove(".git")

        for fname in files:
            if fname.endswith(extensions):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        text = f.read()

                    modified = False
                    for pattern, replacement in patterns:
                        if pattern.search(text):
                            text = pattern.sub(replacement, text)
                            modified = True

                    if modified:
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(text)
                        write_log(app_name, f"   -> [FE Patch] Đã thay thế localhost trong: {os.path.relpath(fpath, fe_dir)}")
                except Exception:
                    pass


# 8. LUỒNG TRIỂN KHAI MONOREPO ĐẦY ĐỦ
def deploy_monorepo_app(repo_url: str, app_name: str, be_domain: str, fe_domain: str, db_type: str = "mysql"):
    write_log(app_name, f"\n=== [BẮT ĐẦU TRIỂN KHAI MONOREPO OPEN SOURCE] {app_name} ===")
    WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}-monorepo"

    try:
        # BƯỚC 1: CLONE REPO
        write_log(app_name, "[1] Đang kéo mã nguồn từ GitHub Monorepo...")
        if os.path.exists(WORKSPACE_DIR):
            run_cmd(["rm", "-rf", WORKSPACE_DIR], app_name)
        run_cmd(["git", "clone", repo_url, WORKSPACE_DIR], app_name)

        # BƯỚC 2: PHÂN TÍCH CẤU TRÚC THƯ MỤC
        be_dir, fe_dir = detect_monorepo_structure(WORKSPACE_DIR)
        rel_be = os.path.relpath(be_dir, WORKSPACE_DIR)
        rel_fe = os.path.relpath(fe_dir, WORKSPACE_DIR) if fe_dir else "Không có (Single BE)"
        write_log(app_name, f"[2] Nhận diện cấu trúc:\n   - Backend  : ./{rel_be}\n   - Frontend : ./{rel_fe}")

        # BƯỚC 3: KHỞI TẠO DATABASE
        write_log(app_name, f"\n[3] Khởi tạo Database rỗng ({db_type.upper()})...")
        db_password = generate_db_password()
        strategy = get_db_strategy(db_type, app_name, db_password, WORKSPACE_DIR)
        generate_db_manifest(app_name, db_password, WORKSPACE_DIR, strategy)

        run_cmd(["kubectl", "apply", "-f", f"{WORKSPACE_DIR}/k8s/db.yaml"], app_name)

        db_name = f"{app_name}_db".replace("-", "_")
        write_log(app_name, "   -> Đang đợi Pod Database sẵn sàng...")
        while True:
            try:
                db_pod_name = subprocess.check_output(
                    f"kubectl get pods -n {K8S_NAMESPACE} -l app={app_name}-db -o jsonpath='{{.items[0].metadata.name}}'",
                    shell=True, text=True
                ).strip()
                status = subprocess.check_output(
                    f"kubectl get pod {db_pod_name} -n {K8S_NAMESPACE} -o jsonpath='{{.status.phase}}'",
                    shell=True, text=True
                ).strip()
                if status == "Running":
                    time.sleep(10)
                    break
            except Exception:
                pass
            time.sleep(5)

        db_info = strategy["connection_info"]
        db_info["password"] = db_password
        db_info["db_name"] = db_name

        db_url = build_database_url(db_info)
        db_info["DATABASE_URL"] = db_url
        db_info["database_url"] = db_url

        extra_envs = extract_env_defaults(be_dir, app_name, fe_domain)
        extra_envs["DATABASE_URL"] = db_url
        write_log(app_name, f"   -> [Config] Đã sinh tự động các cấu hình: {list(extra_envs.keys())}")

        env_file_path = os.path.join(be_dir, ".env")
        try:
            with open(env_file_path, "a+", encoding="utf-8") as f:
                f.seek(0)
                existing = f.read()
                for k, v in extra_envs.items():
                    if f"{k}=" not in existing:
                        f.write(f"\n{k}=\"{v}\"\n")
        except Exception:
            pass

        # BƯỚC 4: XỬ LÝ & BUILD BACKEND
        write_log(app_name, f"\n[4] Cấu hình và Build Backend ({be_domain})...")
        be_port = analyze_and_fix_backend(be_dir, app_name, be_domain, db_info)
        auto_patch_nodejs_esm_conflicts(be_dir, app_name)
        patch_backend_cors(be_dir, fe_domain, app_name)

        BE_IMAGE = f"{DOCKER_USER}/{app_name}-be:latest"
        k8s_be_dir = f"{be_dir}/k8s"
        os.makedirs(k8s_be_dir, exist_ok=True)

        with open(f"{k8s_be_dir}/deployment.yaml", "w", encoding="utf-8") as f:
            f.write(generate_monorepo_be_deployment(f"{app_name}-be", BE_IMAGE, be_port, db_info, db_url, extra_envs))
        with open(f"{k8s_be_dir}/service.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_service(f"{app_name}-be", be_port))
        with open(f"{k8s_be_dir}/ingress.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_ingress(f"{app_name}-be", be_domain))

        base_start = "npm run start:prod"
        pkg_be = os.path.join(be_dir, "package.json")
        if os.path.exists(pkg_be):
            try:
                with open(pkg_be, "r", encoding="utf-8") as f:
                    be_pkg = json.load(f)
                be_scripts = be_pkg.get("scripts", {})
                if "start:prod" in be_scripts:
                    base_start = "npm run start:prod"
                elif "build" in be_scripts:
                    base_start = "node dist/src/main.js || node dist/main.js"
                else:
                    base_start = "npm run start"
            except Exception:
                base_start = "npm run start:prod"

        has_prisma = os.path.exists(os.path.join(be_dir, "prisma"))
        is_lockfile = os.path.exists(os.path.join(be_dir, "package-lock.json"))
        base_install = "npm ci --ignore-scripts" if is_lockfile else "npm install --ignore-scripts"

        if has_prisma:
            install_cmds_str = f'cmds = ["{base_install}", "NODE_OPTIONS=--experimental-require-module npx prisma generate"]'
            # Tự động push cấu trúc bảng và seed data admin (bọc || true để chống lỗi khi pod restart)
            be_start_cmd = f"npx prisma db push && (ENABLE_DEMO_DATA=true npx prisma db seed || true) && {base_start}"
        else:
            install_cmds_str = f'cmds = ["{base_install}"]'
            be_start_cmd = base_start

        write_log(app_name, f"   -> [Config Backend] Start Command: '{be_start_cmd}'")
        nixpacks_be_config = f"""
[variables]
NIXPACKS_NODE_VERSION = "22"
NODE_OPTIONS = "--experimental-require-module"
DATABASE_URL = "{db_url}"
ENABLE_DEMO_DATA = "true"
JWT_SECRET = "{extra_envs.get('JWT_SECRET', '')}"
CORS_ORIGIN = "{extra_envs.get('CORS_ORIGIN', '*')}"
CORS_ORIGINS = "{extra_envs.get('CORS_ORIGINS', '*')}"
FRONTEND_URL = "{extra_envs.get('FRONTEND_URL', '*')}"
CLIENT_URL = "{extra_envs.get('CLIENT_URL', '*')}"

[phases.install]
{install_cmds_str}

[phases.build]
cmds = ["npm run build"]

[start]
cmd = "{be_start_cmd}"
"""
        with open(os.path.join(be_dir, "nixpacks.toml"), "w", encoding="utf-8") as f:
            f.write(nixpacks_be_config.strip())

        run_cmd(["nixpacks", "build", ".", "--name", BE_IMAGE], app_name, cwd=be_dir)
        run_cmd(["docker", "push", BE_IMAGE], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{k8s_be_dir}/deployment.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{k8s_be_dir}/service.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{k8s_be_dir}/ingress.yaml"], app_name)
        write_log(app_name, f"✅ Backend Monorepo online: https://{be_domain}")

        # BƯỚC 5: XỬ LÝ & BUILD FRONTEND
        if fe_dir and os.path.exists(fe_dir):
            write_log(app_name, f"\n[5] Cấu hình và Build Frontend ({fe_domain})...")
            link_frontend_to_backend(fe_dir, app_name, be_domain)
            fix_nextjs_standalone_config(fe_dir, app_name)
            patch_frontend_api_endpoints(fe_dir, be_domain, app_name)
            fe_port = detect_app_port(fe_dir, 3000)

            FE_IMAGE = f"{DOCKER_USER}/{app_name}-fe:latest"
            k8s_fe_dir = f"{fe_dir}/k8s"
            os.makedirs(k8s_fe_dir, exist_ok=True)
            with open(f"{k8s_fe_dir}/deployment.yaml", "w", encoding="utf-8") as f:
                f.write(generate_standard_deployment(f"{app_name}-fe", FE_IMAGE, fe_port, None))
            with open(f"{k8s_fe_dir}/service.yaml", "w", encoding="utf-8") as f:
                f.write(generate_standard_service(f"{app_name}-fe", fe_port))
            with open(f"{k8s_fe_dir}/ingress.yaml", "w", encoding="utf-8") as f:
                f.write(generate_standard_ingress(f"{app_name}-fe", fe_domain))

            be_api_url = f"https://{be_domain}/api"
            be_root_url = f"https://{be_domain}"

            nixpacks_fe_config = f"""
[variables]
NIXPACKS_NODE_VERSION = "22"
NODE_OPTIONS = "--experimental-require-module"
NEXT_PUBLIC_API_URL = "{be_api_url}"
NEXT_PUBLIC_API_BASE_URL = "{be_api_url}"
NEXT_PUBLIC_BACKEND_URL = "{be_root_url}"
NEXT_PUBLIC_SERVER_URL = "{be_root_url}"

[phases.build]
cmds = ["npm run build"]

[start]
cmd = "npm run start"
"""
            with open(os.path.join(fe_dir, "nixpacks.toml"), "w", encoding="utf-8") as f:
                f.write(nixpacks_fe_config.strip())

            run_cmd(["nixpacks", "build", ".", "--name", FE_IMAGE], app_name, cwd=fe_dir)
            run_cmd(["docker", "push", FE_IMAGE], app_name)
            run_cmd(["kubectl", "apply", "-f", f"{k8s_fe_dir}/deployment.yaml"], app_name)
            run_cmd(["kubectl", "apply", "-f", f"{k8s_fe_dir}/service.yaml"], app_name)
            run_cmd(["kubectl", "apply", "-f", f"{k8s_fe_dir}/ingress.yaml"], app_name)
            write_log(app_name, f"✅ Frontend Monorepo online: https://{fe_domain}")

        write_log(app_name, f"\n=== HOÀN TẤT TRIỂN KHAI MONOREPO ===")
        write_log(app_name, f"Backend : https://{be_domain}")
        if fe_dir:
            write_log(app_name, f"Frontend: https://{fe_domain}")
        write_log(app_name, "DONE")

    except Exception as e:
        write_log(app_name, f"\n[LỖI TRIỂN KHAI MONOREPO] {e}")
        write_log(app_name, "ERROR")
