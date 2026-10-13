import os
import subprocess
import time
import random
import string
import json
import re
import shutil
import secrets
from typing import Optional, Dict, Any, List

from builder_core import (
    write_log,
    run_cmd,
    DOCKER_USER,
    BASE_WORKSPACE,
    K8S_NAMESPACE,
    HERMES_API_URL,
    HEADERS,
    FALLBACK_MODELS,
    AGY_MODELS,
    run_agy_json,
    run_agy_prompt,
    query_hermes_ai,
)

# --- HELPER CƠ BẢN VỀ DATABASE & CMS ---

def generate_db_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def generate_wp_salt() -> str:
    """Tạo chuỗi salt an toàn 64 ký tự cho WordPress wp-config.php"""
    chars = string.ascii_letters + string.digits + "!@#$%^&*()-_ []{}~`+=,.;:/?|"
    return ''.join(secrets.choice(chars) for _ in range(64)).replace('\\', '').replace("'", "")

def build_database_url(db_info: dict, host_override: str = None) -> str:
    """Tạo connection string DATABASE_URL chuẩn cho các mã nguồn mở"""
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

def detect_cms_app_port(workspace_dir: str, default_port: int = 80) -> int:
    """Tự động nhận diện cổng ứng dụng cho CMS & Open Source"""
    # 1. WordPress / PHP -> Luôn là port 80
    if os.path.exists(os.path.join(workspace_dir, "wp-config.php")) or \
       os.path.exists(os.path.join(workspace_dir, "wp-config-sample.php")) or \
       os.path.exists(os.path.join(workspace_dir, "index.php")):
        return 80

    # 2. Node.js (Ghost CMS, Strapi, Express, NestJS)
    pkg_path = os.path.join(workspace_dir, "package.json")
    if os.path.exists(pkg_path):
        for env_name in [".env", ".env.local", ".env.production"]:
            env_file = os.path.join(workspace_dir, env_name)
            if os.path.exists(env_file):
                try:
                    with open(env_file, "r", encoding="utf-8") as ef:
                        m = re.search(r'^\s*PORT\s*=\s*(\d+)', ef.read(), re.MULTILINE)
                        if m:
                            return int(m.group(1))
                except Exception:
                    pass
        return 3000

    # 3. Python (Django, Wagtail CMS)
    if os.path.exists(os.path.join(workspace_dir, "requirements.txt")) or \
       os.path.exists(os.path.join(workspace_dir, "manage.py")):
        return 8000

    return default_port

# --- CHIẾN LƯỢC CẤP PHÁT DATABASE STATEFULSET CHO CMS ---

def get_db_strategy(db_type: str, app_name: str, db_password: str) -> dict:
    db_type = db_type.lower()
    app_user = f"user_{app_name}".replace("-", "_")[:16]
    db_name = f"{app_name}_db".replace("-", "_")

    if db_type == "mysql":
        return {
            "image": "mysql:8.0",
            "port": 3306,
            "db_name_env": "MYSQL_DATABASE",
            "db_pass_env": "MYSQL_PASSWORD",
            "db_user": app_user,
            "mount_path": "/var/lib/mysql",
            "extra_env": [
                {"name": "MYSQL_USER", "value": app_user},
                {"name": "MYSQL_ROOT_PASSWORD", "value": generate_db_password(16)}
            ],
            "connection_info": {
                "host": f"{app_name}-db-svc",
                "user": app_user,
                "port": 3306,
                "type": "mysql",
                "db_name": db_name,
                "password": db_password
            }
        }
    elif db_type == "mariadb":
        return {
            "image": "mariadb:10.11",
            "port": 3306,
            "db_name_env": "MARIADB_DATABASE",
            "db_pass_env": "MARIADB_PASSWORD",
            "db_user": app_user,
            "mount_path": "/var/lib/mysql",
            "extra_env": [
                {"name": "MARIADB_USER", "value": app_user},
                {"name": "MARIADB_ROOT_PASSWORD", "value": generate_db_password(16)}
            ],
            "connection_info": {
                "host": f"{app_name}-db-svc",
                "user": app_user,
                "port": 3306,
                "type": "mariadb",
                "db_name": db_name,
                "password": db_password
            }
        }
    elif db_type in ["postgresql", "postgres"]:
        return {
            "image": "postgres:15",
            "port": 5432,
            "db_name_env": "POSTGRES_DB",
            "db_pass_env": "POSTGRES_PASSWORD",
            "db_user": app_user,
            "mount_path": "/var/lib/postgresql/data",
            "extra_env": [{"name": "POSTGRES_USER", "value": app_user}],
            "connection_info": {
                "host": f"{app_name}-db-svc",
                "user": app_user,
                "port": 5432,
                "type": "postgresql",
                "db_name": db_name,
                "password": db_password
            }
        }
    elif db_type == "mongodb":
        return {
            "image": "mongo:6.0",
            "port": 27017,
            "db_name_env": "MONGO_INITDB_DATABASE",
            "db_pass_env": "MONGO_INITDB_ROOT_PASSWORD",
            "db_user": "root",
            "mount_path": "/data/db",
            "extra_env": [{"name": "MONGO_INITDB_ROOT_USERNAME", "value": "root"}],
            "connection_info": {
                "host": f"{app_name}-db-svc",
                "user": "root",
                "port": 27017,
                "type": "mongodb",
                "db_name": db_name,
                "password": db_password
            }
        }
    else:
        raise ValueError(f"Loại Database '{db_type}' chưa được hỗ trợ.")

def generate_cms_db_manifest(app_name: str, db_password: str, workspace_dir: str, strategy: dict):
    db_name = f"{app_name}_db".replace("-", "_")
    env_vars_yaml = f"""
        - name: {strategy['db_pass_env']}
          value: "{db_password}"
        - name: {strategy['db_name_env']}
          value: "{db_name}"
    """
    if "extra_env" in strategy:
        for env in strategy["extra_env"]:
            env_vars_yaml += f"""
        - name: {env['name']}
          value: "{env['value']}"
"""

    yaml_content = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {app_name}-db-pvc
  namespace: {K8S_NAMESPACE}
  labels:
    app: {app_name}-db
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {app_name}-db
  namespace: {K8S_NAMESPACE}
  labels:
    app: {app_name}-db
spec:
  serviceName: "{app_name}-db-svc"
  replicas: 1
  selector:
    matchLabels:
      app: {app_name}-db
  template:
    metadata:
      labels:
        app: {app_name}-db
    spec:
      containers:
      - name: db-container
        image: {strategy['image']}
        env:{env_vars_yaml}
        ports:
        - containerPort: {strategy['port']}
        volumeMounts:
        - name: db-storage
          mountPath: {strategy['mount_path']}
      volumes:
      - name: db-storage
        persistentVolumeClaim:
          claimName: {app_name}-db-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: {app_name}-db-svc
  namespace: {K8S_NAMESPACE}
  labels:
    app: {app_name}-db
spec:
  selector:
    app: {app_name}-db
  ports:
    - protocol: TCP
      port: {strategy['port']}
      targetPort: {strategy['port']}
"""
    k8s_dir = f"{workspace_dir}/k8s"
    os.makedirs(k8s_dir, exist_ok=True)
    with open(f"{k8s_dir}/db.yaml", "w", encoding="utf-8") as f:
        f.write(yaml_content)

# --- CẤU HÌNH TỰ ĐỘNG CHO TỪNG CMS & OPEN-SOURCE ---

def setup_wordpress_turnkey(workspace_dir: str, app_name: str, app_domain: str, db_info: dict) -> bool:
    """
    Tự động chuẩn bị file wp-config.php, cấu hình Database trắng,
    sinh 8 Secret Salt keys, xử lý SSL Traefik reverse proxy và cấu hình Nixpacks PHP 8.2.
    """
    wp_config_path = os.path.join(workspace_dir, "wp-config.php")
    wp_sample_path = os.path.join(workspace_dir, "wp-config-sample.php")

    target_wp = None
    if os.path.exists(wp_config_path):
        target_wp = wp_config_path
    elif os.path.exists(wp_sample_path):
        target_wp = wp_sample_path

    if not target_wp:
        return False

    write_log(app_name, "   -> [WordPress Turnkey] Phát hiện CMS WordPress! Bắt đầu tiêm cấu hình tự động...")
    try:
        with open(target_wp, "r", encoding="utf-8", errors="ignore") as f:
            wp_text = f.read()

        db_name = db_info.get("db_name", f"{app_name}_db")
        db_user = db_info.get("user", "root")
        db_pass = db_info.get("password", "")
        db_host = f"{db_info.get('host', f'{app_name}-db-svc')}:{db_info.get('port', 3306)}"

        # Ghi đè thông số kết nối Database
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_NAME['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_NAME', '{db_name}' );", wp_text)
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_USER['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_USER', '{db_user}' );", wp_text)
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_PASSWORD['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_PASSWORD', '{db_pass}' );", wp_text)
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_HOST['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_HOST', '{db_host}' );", wp_text)

        # Tự sinh 8 khóa bảo mật Salt ngẫu nhiên
        salts = [
            'AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY',
            'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT'
        ]
        for s in salts:
            wp_text = re.sub(
                rf"define\s*\(\s*['\"]{s}['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);",
                f"define( '{s}', '{generate_wp_salt()}' );",
                wp_text
            )

        # Cấu hình SSL Traefik reverse proxy và FS_METHOD direct
        extra_wp_config = f"""
// --- VIBE HOSTING AUTO CONFIGURATION ---
if (!defined('FS_METHOD')) {{
    define( 'FS_METHOD', 'direct' );
}}
if (!defined('WP_MEMORY_LIMIT')) {{
    define( 'WP_MEMORY_LIMIT', '256M' );
}}
if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {{
    $_SERVER['HTTPS'] = 'on';
}}
// ---------------------------------------
"""
        if "/* That's all, stop editing!" in wp_text:
            wp_text = wp_text.replace("/* That's all, stop editing!", extra_wp_config + "\n/* That's all, stop editing!")
        else:
            wp_text += extra_wp_config

        with open(wp_config_path, "w", encoding="utf-8") as f:
            f.write(wp_text)

        # Cấu hình nixpacks.toml chuẩn cho WordPress PHP 8.2 & extensions
        nixpacks_php_cfg = """[phases.setup]
nixPkgs = ["php82", "php82Extensions.mysqli", "php82Extensions.curl", "php82Extensions.gd", "php82Extensions.mbstring", "php82Extensions.xml", "php82Extensions.zip", "php82Extensions.openssl"]

[start]
cmd = "php -S 0.0.0.0:80"
"""
        with open(os.path.join(workspace_dir, "nixpacks.toml"), "w", encoding="utf-8") as nf:
            nf.write(nixpacks_php_cfg)

        write_log(app_name, "   -> [WordPress Turnkey] Đã cấu hình wp-config.php (Secret Salts, SSL Traefik, FS_METHOD direct) và Nixpacks PHP 8.2 thành công!")
        return True
    except Exception as e:
        write_log(app_name, f"   -> [WordPress Turnkey Warning] {e}")
        return False

def setup_opensource_env(workspace_dir: str, app_name: str, app_domain: str, db_info: dict) -> bool:
    """Tự động cấu hình file .env từ .env.example cho các dự án Open-Source chuẩn (Laravel, Node, Django,...)"""
    env_file_path = os.path.join(workspace_dir, ".env")
    env_example_path = os.path.join(workspace_dir, ".env.example")

    if not os.path.exists(env_file_path) and os.path.exists(env_example_path):
        try:
            shutil.copyfile(env_example_path, env_file_path)
            write_log(app_name, "   -> [Auto-Config] Đã khởi tạo file .env từ .env.example")
        except Exception:
            pass

    if os.path.exists(env_file_path):
        try:
            db_type_val = db_info.get("type", "mysql").lower()
            db_conn = "pgsql" if db_type_val in ["postgresql", "postgres"] else ("mongodb" if db_type_val == "mongodb" else "mysql")
            db_url_val = build_database_url(db_info)

            with open(env_file_path, "r", encoding="utf-8", errors="ignore") as ef:
                env_lines = ef.read()

            replacements = {
                "DB_CONNECTION": db_conn,
                "DB_HOST": db_info.get("host", ""),
                "DB_PORT": str(db_info.get("port", 3306)),
                "DB_DATABASE": db_info.get("db_name", ""),
                "DB_USERNAME": db_info.get("user", ""),
                "DB_PASSWORD": db_info.get("password", ""),
                "DATABASE_URL": db_url_val,
                "APP_URL": f"https://{app_domain}"
            }

            for k, v in replacements.items():
                if not v:
                    continue
                if re.search(rf"^{k}\s*=", env_lines, re.MULTILINE):
                    env_lines = re.sub(rf"^{k}\s*=.*", f"{k}={v}", env_lines, flags=re.MULTILINE)
                else:
                    env_lines += f"\n{k}={v}"

            with open(env_file_path, "w", encoding="utf-8") as ef:
                ef.write(env_lines)
            write_log(app_name, f"   -> [Auto-Config .env] Đã cập nhật biến Database ({db_type_val.upper()}) vào file .env!")
            return True
        except Exception as e:
            write_log(app_name, f"   -> [Auto-Config .env Warning] {e}")
    return False

def analyze_and_configure_cms(workspace_dir: str, app_name: str, app_domain: str, db_info: dict, ai_provider: str = "agy") -> int:
    """
    Quy trình tích hợp cấu hình Database vào mã nguồn CMS / Open Source:
    1. WordPress Turnkey (wp-config.php) -> Nhanh, chuẩn 100% không cần AI
    2. Open-Source .env Injection -> Nhanh, chuẩn cho Laravel / Node.js
    3. AI Engine (AGY / Hermes) -> Dành cho mã nguồn tùy biến không theo chuẩn chung
    """
    write_log(app_name, f"[AI ({ai_provider.upper()})] Đang phân tích mã nguồn và cấu hình kết nối DB...")

    # 1. Thử WordPress
    if setup_wordpress_turnkey(workspace_dir, app_name, app_domain, db_info):
        return 80

    # 2. Thử .env thông thường
    setup_opensource_env(workspace_dir, app_name, app_domain, db_info)

    # 3. Quét nhanh ngữ cảnh file cấu hình chính
    code_context = ""
    target_exts = [".json", ".env", ".yaml", ".yml", ".py", ".php", ".js", ".ts"]
    for root, dirs, files in os.walk(workspace_dir):
        # Bỏ qua thư mục rác / thư viện nặng
        dirs[:] = [d for d in dirs if d not in [".git", "node_modules", "vendor", "wp-includes", "wp-admin", "target", ".cache", "dist", "build"]]
        for f in files:
            if any(f.endswith(ext) for ext in target_exts) and f not in ["package-lock.json", "yarn.lock"]:
                fpath = os.path.join(root, f)
                rel_path = os.path.relpath(fpath, workspace_dir)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as cf:
                        content = cf.read()
                        if len(content) < 4000:
                            code_context += f"\n--- FILE: {rel_path} ---\n{content}\n"
                except Exception:
                    pass
        if len(code_context) > 25000:
            break

    # Nếu mã nguồn đã rõ ràng (Node.js/Python/PHP) ta phát hiện cổng trước
    detected_port = detect_cms_app_port(workspace_dir)

    # Nếu có file mã nguồn tùy biến cần AI nối DB
    if code_context.strip():
        db_type_val = db_info.get("type", "mysql").lower()
        prompt = f"""
Bạn là Chuyên gia DevOps cao cấp. Hãy cấu hình ứng dụng Open-Source kết nối vào Database trắng nội bộ K3s.
THÔNG TIN DATABASE NỘI BỘ:
- Loại DB: {db_type_val.upper()}
- Host: {db_info['host']}
- Port: {db_info['port']}
- Tên DB: {db_info['db_name']}
- User: {db_info['user']}
- Password: {db_info['password']}
- Domain Ingress: {app_domain}

MÃ NGUỒN HIỆN TẠI:
{code_context[:20000]}

YÊU CẦU:
Trả về DUY NHẤT một JSON hợp lệ:
{{
  "files_to_update": [
    {{"path": "đường_dẫn_file", "content": "nội_dung_đầy_đủ"}}
  ],
  "target_port": {detected_port}
}}
"""
        try:
            if ai_provider == "hermes":
                res_ai = query_hermes_ai(prompt, app_name)
            else:
                res_ai = run_agy_json(prompt, app_name, workspace_dir=workspace_dir)

            if isinstance(res_ai, dict):
                for f_item in res_ai.get("files_to_update", []):
                    rel_p = f_item.get("path")
                    f_cnt = f_item.get("content")
                    if rel_p and f_cnt:
                        abs_p = os.path.join(workspace_dir, rel_p)
                        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                        with open(abs_p, "w", encoding="utf-8") as out_f:
                            out_f.write(f_cnt)
                        write_log(app_name, f"   -> [AI Config] Đã cập nhật file cấu hình: {rel_p}")
                if res_ai.get("target_port"):
                    detected_port = int(res_ai["target_port"])
        except Exception as e:
            write_log(app_name, f"   -> [AI Config Note] AI bỏ qua bước tùy biến thêm: {e}")

    return detected_port

# --- TEMPLATES DEPLOYMENT, SERVICE & INGRESS KUBERNETES ---

def generate_cms_deployment(app_name: str, image_tag: str, target_port: int, db_info: dict) -> str:
    db_type = db_info.get("type", "").lower()
    db_host = db_info.get("host", "")
    db_port = str(db_info.get("port", 3306))
    db_user = db_info.get("user", "")
    db_pass = db_info.get("password", "")
    db_name = db_info.get("db_name", "")

    env_lines = [
        f'        - name: PORT\n          value: "{target_port}"',
        f'        - name: SERVER_PORT\n          value: "{target_port}"',
        f'        - name: DB_HOST\n          value: "{db_host}"',
        f'        - name: DB_PORT\n          value: "{db_port}"',
        f'        - name: DB_USER\n          value: "{db_user}"',
        f'        - name: DB_PASS\n          value: "{db_pass}"',
        f'        - name: DB_PASSWORD\n          value: "{db_pass}"',
        f'        - name: DB_NAME\n          value: "{db_name}"',
        f'        - name: DATABASE_URL\n          value: "{build_database_url(db_info)}"',
    ]

    env_yaml = "\n".join(env_lines)

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
      imagePullSecrets:
      - name: regcred
      containers:
      - name: {app_name}
        image: {image_tag}
        imagePullPolicy: Always
        ports:
        - containerPort: {target_port}
        env:
{env_yaml}
"""

def generate_cms_service(app_name: str, target_port: int) -> str:
    return f"""apiVersion: v1
kind: Service
metadata:
  name: {app_name}-svc
  namespace: {K8S_NAMESPACE}
  labels:
    app: {app_name}
spec:
  selector:
    app: {app_name}
  ports:
    - protocol: TCP
      port: 80
      targetPort: {target_port}
"""

def generate_cms_ingress(app_name: str, app_domain: str) -> str:
    return f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {app_name}-ingress
  namespace: {K8S_NAMESPACE}
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  tls:
  - hosts:
    - {app_domain}
    secretName: {app_name}-tls
  rules:
  - host: {app_domain}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: {app_name}-svc
            port:
              number: 80
"""

# --- HÀM DỌN DẸP SẠCH SẼ TÀI NGUYÊN CMS & DATABASE ---

def delete_cms_db_resources(app_name: str):
    try:
        write_log(app_name, f"[Cleanup] Đang dọn dẹp cụm CMS {app_name} (Database, Deployment, Ingress)...")
        subprocess.run(["kubectl", "delete", "statefulset", f"{app_name}-db", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "service", f"{app_name}-db-svc", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "pvc", f"{app_name}-db-pvc", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "pvc", f"db-storage-{app_name}-db-0", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "deployment,service,ingress", app_name, f"{app_name}-svc", f"{app_name}-ingress", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "all,pvc", "-l", f"app={app_name}-db", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        write_log(app_name, f"[Cleanup] Đã dọn dẹp hoàn tất toàn bộ tài nguyên CMS của {app_name}!")
    except Exception as e:
        write_log(app_name, f"[CẢNH BÁO Cleanup] Lỗi: {e}")

# --- QUY TRÌNH CHÍNH: TRIỂN KHAI CMS & OPEN-SOURCE (CLEAN DB) ---

def deploy_cms_app(
    repo_url: str,
    app_name: str,
    app_domain: str,
    db_type: str = "mysql",
    ai_provider: str = "agy",
    cms_type: str = "wordpress"
):
    """
    Quy trình triển khai trọn gói CMS (WordPress mặc định) hoặc bất kỳ Open-Source repo nào
    với Clean Database (Database Trắng) tự động, không yêu cầu người dùng nạp file backup SQL.
    """
    write_log(app_name, f"\n=== [BẮT ĐẦU TRIỂN KHAI CMS / OPEN-SOURCE] {app_name} ({db_type.upper()}) (AI: {ai_provider.upper()}) ===")
    WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"

    try:
        # 0. Chuẩn bị mã nguồn
        target_repo = (repo_url or "").strip()
        if not target_repo or target_repo.lower() in ["wordpress", "wp", "default"] or cms_type == "wordpress":
            final_repo_url = "https://github.com/WordPress/WordPress.git"
            write_log(app_name, "[0] Nhận diện CMS WordPress Mặc định -> Kéo mã nguồn chính thức từ https://github.com/WordPress/WordPress.git...")
        else:
            final_repo_url = target_repo
            write_log(app_name, f"[0] Đang kéo mã nguồn từ GitHub: {final_repo_url}...")

        if os.path.exists(WORKSPACE_DIR):
            run_cmd(["rm", "-rf", WORKSPACE_DIR], app_name)
        # Sử dụng shallow clone --depth 1 để tải nhanh tức thì
        run_cmd(["git", "clone", "--depth", "1", final_repo_url, WORKSPACE_DIR], app_name)

        # 1. Chuẩn bị Database trắng nội bộ
        write_log(app_name, f"\n[1] Đang chuẩn bị Database nội bộ ({db_type.upper()})...")
        db_password = generate_db_password(14)
        db_strategy = get_db_strategy(db_type, app_name, db_password)
        generate_cms_db_manifest(app_name, db_password, WORKSPACE_DIR, db_strategy)

        # 2. Triển khai Database lên cụm K8s
        write_log(app_name, "[2] Đang triển khai StatefulSet Database...")
        run_cmd(["kubectl", "apply", "-f", f"{WORKSPACE_DIR}/k8s/db.yaml"], app_name)

        # 3. Đợi Pod Database khởi động và kiểm tra kết nối
        write_log(app_name, "[3] Đang chờ Pod Database khởi động...")
        db_pod_name = f"{app_name}-db-0"
        db_ready = False
        for attempt in range(30):
            try:
                chk = subprocess.run(
                    f"kubectl get pod {db_pod_name} -n {K8S_NAMESPACE} -o jsonpath='{{.status.phase}}'",
                    shell=True, capture_output=True, text=True, timeout=5
                )
                if chk.stdout.strip() == "Running":
                    db_ready = True
                    break
            except Exception:
                pass
            time.sleep(3)

        if not db_ready:
            write_log(app_name, "⚠️ [Cảnh báo] Database chưa báo Running sau 90s, tiếp tục tiến trình...")
        else:
            write_log(app_name, f"✅ Database đã sẵn sàng: {db_pod_name}")

        # 4. Ghi nhận trạng thái Clean DB
        write_log(app_name, f"ℹ️ [DB Status] Chế độ Database Trắng (Clean DB) - Đã khởi tạo {db_type.upper()} sạch. Không có file backup, bỏ qua bước nạp dữ liệu.")

        # 5. Cấu hình Database vào mã nguồn
        write_log(app_name, f"\n[4] Đang cấu hình kết nối Database vào mã nguồn ({cms_type.upper()})...")
        db_info = db_strategy["connection_info"]
        target_port = analyze_and_configure_cms(WORKSPACE_DIR, app_name, app_domain, db_info, ai_provider)
        write_log(app_name, f"   -> Cổng dịch vụ được xác định: {target_port}")

        # 6. Đóng gói Docker Image bằng Nixpacks
        IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
        write_log(app_name, f"\n[5] Đang build Docker Image ({IMAGE_TAG}) bằng Nixpacks...")
        run_cmd(["nixpacks", "build", WORKSPACE_DIR, "--name", IMAGE_TAG], app_name)

        write_log(app_name, "\n[6] Đang đẩy Image lên Docker Hub...")
        run_cmd(["docker", "push", IMAGE_TAG], app_name)

        # 7. Khởi tạo K8s manifests cho ứng dụng
        write_log(app_name, "\n[7] Đang tạo cấu hình Kubernetes Manifests...")
        k8s_dir = f"{WORKSPACE_DIR}/k8s"
        os.makedirs(k8s_dir, exist_ok=True)

        deployment_yaml = generate_cms_deployment(app_name, IMAGE_TAG, target_port, db_info)
        service_yaml = generate_cms_service(app_name, target_port)
        ingress_yaml = generate_cms_ingress(app_name, app_domain)

        with open(f"{k8s_dir}/deployment.yaml", "w", encoding="utf-8") as f:
            f.write(deployment_yaml)
        with open(f"{k8s_dir}/service.yaml", "w", encoding="utf-8") as f:
            f.write(service_yaml)
        with open(f"{k8s_dir}/ingress.yaml", "w", encoding="utf-8") as f:
            f.write(ingress_yaml)

        # 8. Triển khai ứng dụng lên Kubernetes
        write_log(app_name, "\n[8] Đang triển khai ứng dụng lên Kubernetes...")
        run_cmd(["kubectl", "apply", "-f", f"{k8s_dir}/deployment.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{k8s_dir}/service.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{k8s_dir}/ingress.yaml"], app_name)

        write_log(app_name, f"\n[9] Khởi động lại deployment để áp dụng phiên bản mới...")
        subprocess.run(["kubectl", "rollout", "restart", f"deployment/{app_name}", "-n", K8S_NAMESPACE], check=False)

        write_log(app_name, f"\n=== HOÀN TẤT TRIỂN KHAI CMS / OPEN SOURCE THÀNH CÔNG ===")
        write_log(app_name, f"🌐 Truy cập ứng dụng tại: https://{app_domain}")
        write_log(app_name, f"🗄️ Database ({db_type.upper()}): {db_info['host']} (Database: {db_info['db_name']}, User: {db_info['user']})")
        write_log(app_name, "DONE")

    except Exception as e:
        write_log(app_name, f"\n[LỖI NGHIÊM TRỌNG CMS DEPLOY] {e}")
        write_log(app_name, "ERROR")
