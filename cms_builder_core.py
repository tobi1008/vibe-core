"""
CMS & Open-Source Builder Core
Triển khai CMS (WordPress, Ghost, v.v.) hoặc bất kỳ repository mã nguồn mở nào từ GitHub
với Clean Database (Database Trắng tự động) - không cần file backup SQL.
Tự động phân tích mã nguồn, tạo Pod Database tương ứng, kết nối Database vào mã nguồn,
đóng gói Nixpacks và triển khai lên Kubernetes K3s.
"""

import os
import re
import shutil
import secrets
import string
import subprocess
import time
from typing import Optional

from builder_core import write_log, run_cmd, DOCKER_USER, K8S_NAMESPACE, BASE_WORKSPACE
from db_builder_core import (
    generate_db_password,
    get_db_strategy,
    generate_db_manifest,
    analyze_and_fix_backend,
    auto_patch_nodejs_esm_conflicts,
    generate_standard_deployment,
    generate_standard_service,
    generate_standard_ingress,
    detect_app_port,
)


def generate_wp_salt() -> str:
    """Tạo chuỗi salt ngẫu nhiên an toàn 64 ký tự cho WordPress wp-config.php"""
    chars = string.ascii_letters + string.digits + "!@#$%^&*()-_ []{}~`+=,.;:/?|"
    return ''.join(secrets.choice(chars) for _ in range(64)).replace('\\', '').replace("'", "")


def build_database_url(db_info: dict) -> str:
    """Tạo chuỗi connection string DATABASE_URL cho mã nguồn mở"""
    db_type = db_info.get("type", "mysql").lower()
    user = db_info.get("user", "root")
    password = db_info.get("password", "")
    host = db_info.get("host", "localhost")
    port = db_info.get("port", 3306)
    db_name = db_info.get("db_name", "")

    if db_type in ["mysql", "mariadb"]:
        return f"mysql://{user}:{password}@{host}:{port}/{db_name}"
    elif db_type in ["postgresql", "postgres"]:
        return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
    elif db_type == "mongodb":
        return f"mongodb://{user}:{password}@{host}:{port}/{db_name}?authSource=admin"
    return ""


def setup_wordpress_config(workspace_dir: str, app_name: str, db_info: dict) -> bool:
    """
    Tự động cấu hình toàn diện cho WordPress:
    - Điền thông tin kết nối Database vào wp-config.php
    - Sinh 8 khóa bảo mật Salt ngẫu nhiên
    - Xử lý SSL Traefik reverse proxy (chống vòng lặp chuyển hướng HTTPS)
    - Cấu hình FS_METHOD direct (cài plugin/theme trực tiếp không cần FTP)
    - Tạo cấu hình Nixpacks chuẩn PHP 8.2 + mysqli
    Trả về True nếu phát hiện và cấu hình WordPress thành công.
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

    write_log(app_name, "   -> [WordPress Setup] Phát hiện mã nguồn WordPress! Đang tự động cấu hình Database...")

    try:
        with open(target_wp, "r", encoding="utf-8", errors="ignore") as f:
            wp_text = f.read()

        db_name = db_info.get("db_name", f"{app_name}_db")
        db_user = db_info.get("user", "root")
        db_pass = db_info.get("password", "")
        db_host = f"{db_info.get('host', f'{app_name}-db-svc')}:{db_info.get('port', 3306)}"

        # 1. Điền thông số Database
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_NAME['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_NAME', '{db_name}' );", wp_text)
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_USER['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_USER', '{db_user}' );", wp_text)
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_PASSWORD['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_PASSWORD', '{db_pass}' );", wp_text)
        wp_text = re.sub(r"define\s*\(\s*['\"]DB_HOST['\"]\s*,\s*['\"][^'\"]*['\"]\s*\);", f"define( 'DB_HOST', '{db_host}' );", wp_text)

        # 2. Sinh 8 khóa bảo mật Salt ngẫu nhiên
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

        # 3. Cấu hình SSL Traefik reverse proxy + FS_METHOD direct
        extra_wp_config = """
// --- VIBE HOSTING AUTO CONFIGURATION ---
if (!defined('FS_METHOD')) {
    define( 'FS_METHOD', 'direct' );
}
if (!defined('WP_MEMORY_LIMIT')) {
    define( 'WP_MEMORY_LIMIT', '256M' );
}
if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {
    $_SERVER['HTTPS'] = 'on';
}
// ---------------------------------------
"""
        if "/* That's all, stop editing!" in wp_text:
            wp_text = wp_text.replace("/* That's all, stop editing!", extra_wp_config + "\n/* That's all, stop editing!")
        else:
            wp_text += extra_wp_config

        with open(wp_config_path, "w", encoding="utf-8") as f:
            f.write(wp_text)

        # 4. Tạo nixpacks.toml chuẩn PHP 8.2 + mysqli
        nixpacks_php_cfg = """[phases.setup]
nixPkgs = ["php82", "php82Extensions.mysqli", "php82Extensions.curl", "php82Extensions.gd", "php82Extensions.mbstring", "php82Extensions.xml", "php82Extensions.zip", "php82Extensions.openssl"]

[start]
cmd = "php -S 0.0.0.0:80"
"""
        with open(os.path.join(workspace_dir, "nixpacks.toml"), "w", encoding="utf-8") as nf:
            nf.write(nixpacks_php_cfg)

        write_log(app_name, "   -> [WordPress Setup] Đã cấu hình wp-config.php (Database, Secret Salts, SSL Traefik, FS_METHOD direct) và Nixpacks PHP 8.2 thành công!")
        return True

    except Exception as e:
        write_log(app_name, f"   -> [WordPress Setup Warning] {e}")
        return False


def setup_opensource_env(workspace_dir: str, app_name: str, app_domain: str, db_info: dict) -> bool:
    """
    Tự động chuẩn bị và tiêm cấu hình Database vào file .env cho các dự án Open-Source chuẩn (Laravel, Node, Django,...).
    """
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


# ============================================================
# HÀM CHÍNH: DEPLOY CMS / OPEN-SOURCE VỚI CLEAN DATABASE
# ============================================================

def deploy_cms_app(
    repo_url: str,
    app_name: str,
    app_domain: str,
    db_type: str = "mysql",
    ai_provider: str = "agy",
    cms_type: Optional[str] = None
):
    """
    Quy trình triển khai CMS / Open-Source:
    1. Kéo mã nguồn từ link GitHub
    2. Tự khởi tạo Pod Database sạch (Clean DB) trên K3s
    3. Tự động phân tích mã nguồn và kết nối Database vào ứng dụng
    4. Build image bằng Nixpacks và khởi chạy trên K8s
    """
    target_repo = (repo_url or "").strip()
    if not target_repo or target_repo.lower() in ["wordpress", "wp", "default"]:
        final_repo_url = "https://github.com/WordPress/WordPress.git"
    else:
        final_repo_url = target_repo

    write_log(app_name, f"\n=== [BẮT ĐẦU TRIỂN KHAI CMS / MÃ NGUỒN MỞ] {app_name} ({db_type.upper()}) (AI: {ai_provider.upper()}) ===")
    WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"

    try:
        # [0] Kéo mã nguồn từ GitHub
        write_log(app_name, f"[0] Đang kéo mã nguồn từ GitHub: {final_repo_url}...")
        if os.path.exists(WORKSPACE_DIR):
            run_cmd(["rm", "-rf", WORKSPACE_DIR], app_name)
        run_cmd(["git", "clone", "--depth", "1", final_repo_url, WORKSPACE_DIR], app_name)

        # [1] Tự động tạo Pod Database nội bộ trên K3s
        write_log(app_name, f"\n[1] Đang chuẩn bị Database nội bộ ({db_type.upper()})...")
        pass_env_map = {"mysql": "MYSQL_PASSWORD", "mariadb": "MARIADB_PASSWORD", "postgresql": "POSTGRES_PASSWORD", "mongodb": "MONGO_INITDB_ROOT_PASSWORD"}
        db_password = ""
        target_env = pass_env_map.get(db_type.lower(), "MYSQL_PASSWORD")
        try:
            check_pass_cmd = f"kubectl get statefulset {app_name}-db -n {K8S_NAMESPACE} -o jsonpath='{{.spec.template.spec.containers[0].env[?(@.name==\"{target_env}\")].value}}'"
            existing_pass = subprocess.check_output(check_pass_cmd, shell=True, text=True).strip()
            if existing_pass:
                db_password = existing_pass
        except Exception:
            pass
        if not db_password:
            db_password = generate_db_password()

        strategy = get_db_strategy(db_type, app_name, db_password, WORKSPACE_DIR)
        generate_db_manifest(app_name, db_password, WORKSPACE_DIR, strategy)

        # [2] Triển khai StatefulSet Database lên K3s
        write_log(app_name, f"[2] Đang triển khai Pod Database ({db_type.upper()}) lên K3s...")
        run_cmd(["kubectl", "apply", "-f", "k8s/db.yaml"], app_name, cwd=WORKSPACE_DIR)

        # [3] Đợi Pod Database sẵn sàng
        write_log(app_name, "[3] Đang chờ Pod Database khởi động...")
        db_pod_name = ""
        while True:
            try:
                db_pod_name = subprocess.check_output(f"kubectl get pods -n {K8S_NAMESPACE} -l app={app_name}-db -o jsonpath='{{.items[0].metadata.name}}'", shell=True, text=True).strip()
                status = subprocess.check_output(f"kubectl get pod {db_pod_name} -n {K8S_NAMESPACE} -o jsonpath='{{.status.phase}}'", shell=True, text=True).strip()
                if status == "Running":
                    time.sleep(12)
                    break
            except Exception:
                pass
            time.sleep(4)
        write_log(app_name, f"✅ Database đã sẵn sàng: {db_pod_name}")

        db_name = f"{app_name}_db".replace("-", "_")
        write_log(app_name, f"ℹ️ [DB Status] Chế độ Database Trắng (Clean DB) - Đã khởi tạo {db_type.upper()} sạch. Người dùng có thể thiết lập ứng dụng trực tiếp.")

        db_info = strategy["connection_info"]
        db_info["password"] = db_password
        db_info["db_name"] = db_name

        # [4] Phân tích mã nguồn và kết nối Database vào ứng dụng
        write_log(app_name, "\n[4] Đang phân tích mã nguồn và cấu hình kết nối Database...")

        # Nếu phát hiện WordPress thuần (có wp-config.php / wp-config-sample.php hoặc cms_type là wordpress)
        is_wp_repo = os.path.exists(os.path.join(WORKSPACE_DIR, "wp-config.php")) or \
                     os.path.exists(os.path.join(WORKSPACE_DIR, "wp-config-sample.php")) or \
                     (cms_type and cms_type.lower() == "wordpress")

        if is_wp_repo:
            setup_wordpress_config(WORKSPACE_DIR, app_name, db_info)
            write_log(app_name, "   -> [WordPress Setup] Đã khởi tạo cấu hình wp-config.php, Salt, SSL Traefik và Nixpacks PHP 8.2.")

        # Chuẩn bị file môi trường cơ sở từ .env.example nếu chưa có .env (giúp AI có template cấu trúc biến)
        env_file_path = os.path.join(WORKSPACE_DIR, ".env")
        env_example_path = os.path.join(WORKSPACE_DIR, ".env.example")
        if not os.path.exists(env_file_path) and os.path.exists(env_example_path):
            try:
                shutil.copyfile(env_example_path, env_file_path)
                write_log(app_name, "   -> [Auto-Prep] Đã khởi tạo file .env từ .env.example để AI phân tích")
            except Exception:
                pass

        # LUÔN LUÔN gọi AI Engine (AGY / Hermes) phân tích và cấu hình kết nối Database trước (Cách 2).
        # Nếu không có model nào trả về (lỗi/timeout), analyze_and_fix_backend sẽ tự động fallback sang cấu hình mặc định để Nixpacks tự làm việc!
        detected_port = analyze_and_fix_backend(WORKSPACE_DIR, app_name, app_domain, db_info, ai_provider=ai_provider)

        auto_patch_nodejs_esm_conflicts(WORKSPACE_DIR, app_name)

        # [5] Build Docker Image bằng Nixpacks
        IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
        k8s_dir = f"{WORKSPACE_DIR}/k8s"
        with open(f"{k8s_dir}/deployment.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_deployment(app_name, IMAGE_TAG, detected_port, db_info))
        with open(f"{k8s_dir}/service.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_service(app_name, detected_port))
        with open(f"{k8s_dir}/ingress.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_ingress(app_name, app_domain))

        write_log(app_name, f"\n[5] Đang Build Docker Image ({IMAGE_TAG})...")
        run_cmd(["nixpacks", "build", ".", "--name", IMAGE_TAG], app_name, cwd=WORKSPACE_DIR)

        write_log(app_name, "[6] Đang đẩy Image lên Docker Hub...")
        run_cmd(["docker", "push", IMAGE_TAG], app_name)

        # [6] Khởi chạy Pod Ứng dụng trên K3s
        write_log(app_name, "[7] Đang khởi chạy Ứng dụng Web trên K3s...")
        run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/service.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/ingress.yaml"], app_name, cwd=WORKSPACE_DIR)

        write_log(app_name, f"\n=== HOÀN TẤT TRIỂN KHAI CMS / OPEN-SOURCE ===")
        write_log(app_name, f"🌐 Truy cập ứng dụng: https://{app_domain}")
        write_log(app_name, f"🗄️ Database ({db_type.upper()}): {db_info['host']} (DB: {db_name}, User: {db_info['user']})")
        write_log(app_name, "DONE")

    except Exception as e:
        write_log(app_name, f"\n[LỖI NGHIÊM TRỌNG CMS DEPLOY] {e}")
        write_log(app_name, "ERROR")
