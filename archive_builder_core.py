import os
import shutil
import subprocess
import json
import re
import time
import zipfile
import tarfile
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

from builder_core import (
    K8S_NAMESPACE,
    DOCKER_USER,
    DOCKER_PASSWORD,
    BASE_WORKSPACE,
    LOGS_DIR,
    write_log,
    run_cmd,
    run_agy_json,
    delete_app,
)

UPLOAD_DIR = "/opt/vibe-hosting/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(BASE_WORKSPACE, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)


# ==============================================================================
# 1. BỘ MÁY GIẢI NÉN ĐA ĐỊNH DẠNG AN TOÀN (ZIP, TAR, GZ, BZ2, XZ, RAR, 7Z)
# ==============================================================================

def safe_extract_zip(zip_path: str, extract_dir: str):
    """Giải nén ZIP có kèm cơ chế chống tấn công Zip Slip (Path Traversal)"""
    abs_extract_dir = os.path.abspath(extract_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target_path = os.path.abspath(os.path.join(abs_extract_dir, member.filename))
            if not target_path.startswith(abs_extract_dir + os.sep) and target_path != abs_extract_dir:
                raise Exception(f"Phát hiện rủi ro Path Traversal trong file ZIP: {member.filename}")
        zf.extractall(extract_dir)


def safe_extract_tar(tar_path: str, extract_dir: str):
    """Giải nén TAR / TAR.GZ / TAR.BZ2 / TAR.XZ an toàn"""
    abs_extract_dir = os.path.abspath(extract_dir)
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            target_path = os.path.abspath(os.path.join(abs_extract_dir, member.name))
            if not target_path.startswith(abs_extract_dir + os.sep) and target_path != abs_extract_dir:
                raise Exception(f"Phát hiện rủi ro Path Traversal trong file TAR: {member.name}")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(extract_dir, filter="data")
        else:
            tf.extractall(extract_dir)


def extract_rar(rar_path: str, extract_dir: str, app_name: str):
    """Giải nén file RAR bằng thư viện Python hoặc lệnh unrar/7z của hệ thống"""
    # Thử dùng rarfile nếu có
    try:
        import rarfile
        with rarfile.RarFile(rar_path) as rf:
            rf.extractall(extract_dir)
            return
    except Exception:
        pass

    # Fallback qua lệnh CLI
    if shutil.which("unrar"):
        run_cmd(["unrar", "x", "-o+", "-inul", rar_path, extract_dir], app_name)
    elif shutil.which("rar"):
        run_cmd(["rar", "x", "-o+", "-inul", rar_path, extract_dir], app_name)
    elif shutil.which("7z"):
        run_cmd(["7z", "x", "-y", f"-o{extract_dir}", rar_path], app_name)
    else:
        raise Exception("Hệ thống chưa cài đặt 'unrar' hoặc '7z'. Vui lòng cài gói: apt install unrar p7zip-full")


def extract_7z(archive_path: str, extract_dir: str, app_name: str):
    """Giải nén file 7Z"""
    try:
        import py7zr
        with py7zr.SevenZipFile(archive_path, mode="r") as z:
            z.extractall(path=extract_dir)
            return
    except Exception:
        pass

    if shutil.which("7z"):
        run_cmd(["7z", "x", "-y", f"-o{extract_dir}", archive_path], app_name)
    elif shutil.which("7za"):
        run_cmd(["7za", "x", "-y", f"-o{extract_dir}", archive_path], app_name)
    else:
        raise Exception("Hệ thống chưa cài đặt '7z'. Vui lòng cài gói: apt install p7zip-full")


def flatten_single_root_dir(extract_dir: str, app_name: str):
    """
    Tự động chuẩn hóa cấu trúc thư mục sau giải nén:
    Nếu người dùng nén cả thư mục cha (ví dụ repo-main.zip chứa thư mục repo-main/),
    hàm này sẽ tự động đưa toàn bộ file/folder bên trong ra ngoài thư mục gốc để Nixpacks nhận diện.
    """
    # Xóa rác metadata macOS nếu có
    macosx_dir = os.path.join(extract_dir, "__MACOSX")
    if os.path.exists(macosx_dir):
        shutil.rmtree(macosx_dir, ignore_errors=True)

    ds_store = os.path.join(extract_dir, ".DS_Store")
    if os.path.exists(ds_store):
        try:
            os.remove(ds_store)
        except Exception:
            pass

    entries = [e for e in os.listdir(extract_dir) if e not in ["__MACOSX", ".DS_Store"]]
    if len(entries) == 1:
        single_path = os.path.join(extract_dir, entries[0])
        if os.path.isdir(single_path):
            write_log(app_name, f"📁 Phát hiện thư mục lồng [{entries[0]}], đang tự động chuyển toàn bộ file ra thư mục gốc...")
            temp_sub = os.path.join(extract_dir, f"_flatten_temp_{int(time.time())}")
            os.rename(single_path, temp_sub)
            for item in os.listdir(temp_sub):
                src = os.path.join(temp_sub, item)
                dst = os.path.join(extract_dir, item)
                shutil.move(src, dst)
            shutil.rmtree(temp_sub, ignore_errors=True)


def extract_archive(archive_path: str, extract_to: str, app_name: str):
    """Hàm giải nén tổng hợp: Tự động phát hiện định dạng file và giải nén an toàn"""
    if not os.path.exists(archive_path):
        raise FileNotFoundError(f"Không tìm thấy file nén tại: {archive_path}")

    os.makedirs(extract_to, exist_ok=True)
    lower_path = archive_path.lower()

    write_log(app_name, f"[1] Đang giải nén file mã nguồn: {os.path.basename(archive_path)}...")

    if lower_path.endswith(".zip"):
        safe_extract_zip(archive_path, extract_to)
    elif lower_path.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2", ".tar.xz", ".txz", ".tar")):
        safe_extract_tar(archive_path, extract_to)
    elif lower_path.endswith(".rar"):
        extract_rar(archive_path, extract_to, app_name)
    elif lower_path.endswith(".7z"):
        extract_7z(archive_path, extract_to, app_name)
    else:
        # Thử fallback qua 7z hoặc tar
        if shutil.which("7z"):
            run_cmd(["7z", "x", "-y", f"-o{extract_to}", archive_path], app_name)
        elif zipfile.is_zipfile(archive_path):
            safe_extract_zip(archive_path, extract_to)
        elif tarfile.is_tarfile(archive_path):
            safe_extract_tar(archive_path, extract_to)
        else:
            raise ValueError(f"Định dạng file nén không được hỗ trợ: {os.path.basename(archive_path)}")

    # Làm phẳng thư mục nếu bị lồng
    flatten_single_root_dir(extract_to, app_name)
    write_log(app_name, f"✅ Giải nén và chuẩn hóa cấu trúc thư mục thành công!")


# ==============================================================================
# 2. HỆ THỐNG PYTHON TỰ ĐỘNG VÁ LỖI MÃ NGUỒN (AUTO-HEAL 0.0.0.0)
# ==============================================================================

def auto_heal_network_bindings(workspace_dir: str, app_name: str):
    """Quét và sửa lỗi IP nội bộ 127.0.0.1 / localhost -> 0.0.0.0 để container nhận traffic"""
    write_log(app_name, "[1.5] [AUTO-HEAL] Đang tự động quét và sửa lỗi IP nội bộ (localhost -> 0.0.0.0)...")
    for root, dirs, files in os.walk(workspace_dir):
        if any(ign in root for ign in [".git", "node_modules", "venv", ".next", "dist", "build"]):
            continue
        for file in files:
            if file.endswith((".js", ".ts", ".py", ".json", ".go", ".java", ".php")):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()

                    modified = False
                    if "127.0.0.1" in content:
                        content = content.replace("127.0.0.1", "0.0.0.0")
                        modified = True
                    if "'localhost'" in content:
                        content = content.replace("'localhost'", "'0.0.0.0'")
                        modified = True
                    if '"localhost"' in content:
                        content = content.replace('"localhost"', '"0.0.0.0"')
                        modified = True

                    if modified:
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(content)
                        write_log(app_name, f" 🛠️ [PYTHON] Đã vá IP 0.0.0.0 thành công trong file: {file}")
                except Exception:
                    pass


# ==============================================================================
# 3. LUỒNG TRIỂN KHAI MÃ NGUỒN TỪ FILE NÉN (STANDALONE WEB APP)
# ==============================================================================

def deploy_archive_app(archive_path: str, app_name: str, app_domain: str):
    """
    Quy trình triển khai hoàn chỉnh ứng dụng từ file nén tải lên (.zip, .tar, .rar, .7z)
    Tích hợp Antigravity AI Engine (Gemini Flash) sinh cấu hình và tự phục hồi.
    """
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(f"{LOGS_DIR}/{app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"=== [BẮT ĐẦU DEPLOY TỪ FILE NÉN] {app_name} ===\n")

    IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
    WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"

    os.makedirs(BASE_WORKSPACE, exist_ok=True)
    if os.path.exists(WORKSPACE_DIR):
        shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)

    try:
        # Bước 1: Giải nén file vào workspace
        extract_archive(archive_path, WORKSPACE_DIR, app_name)

        # Xóa file nén tải lên để tiết kiệm dung lượng ổ cứng
        if os.path.exists(archive_path):
            try:
                os.remove(archive_path)
            except Exception:
                pass

        # Bước 1.5: Vá lỗi IP localhost -> 0.0.0.0
        auto_heal_network_bindings(WORKSPACE_DIR, app_name)

        # Bước 2: Quét cấu trúc mã nguồn
        write_log(app_name, "[2] Đang quét cấu trúc mã nguồn dự án...")
        tree_output = run_cmd(["tree", "-I", "node_modules|.git|venv|dist|build", WORKSPACE_DIR], app_name)
        code_context = tree_output

        core_files = [
            "package.json", "requirements.txt", "composer.json", "go.mod", "pom.xml",
            "app.py", "main.py", "index.js", "server.js",
            "vite.config.js", "vite.config.ts", "Dockerfile"
        ]

        for config_file in core_files:
            file_path = os.path.join(WORKSPACE_DIR, config_file)
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    code_context += f"\n\n--- Nội dung {config_file} ---\n" + f.read()

        MAX_RETRIES = 3
        build_error = ""

        # Bước 3: Antigravity AI (Gemini Flash) phân tích và sinh cấu hình
        for attempt in range(1, MAX_RETRIES + 1):
            write_log(app_name, f"\n[3] (Lần thử {attempt}/{MAX_RETRIES}) Bắt đầu gọi Antigravity AI phân tích mã nguồn...")

            system_prompt = f"""
Bạn là AI DevOps Agent cho Vibe Hosting. Đọc mã nguồn, sửa các lỗi cơ bản và sinh cấu hình.
You must respond with valid JSON only. No markdown code blocks.
TUYỆT ĐỐI KHÔNG SỬ DỤNG BẤT KỲ TOOL NÀO (NO TOOL CALLS). KHÔNG ĐƯỢC PHẢN HỒI BẰNG VĂN BẢN THƯỜNG.

BẮT BUỘC trả về JSON theo cấu trúc:
{{
  "nixpacks_toml": "nội dung file nixpacks.toml",
  "k3s_deployment": "nội dung file deployment.yaml",
  "k3s_service": "nội dung file service.yaml",
  "k3s_ingress": "nội dung file ingress.yaml",
  "auto_patches": [
    {{
      "file_name": "tên file cần sửa",
      "new_content": "nội dung mới sau khi sửa"
    }}
  ],
  "user_action_required": ""
}}

QUY ĐỊNH AUTO-PATCH (CỰC KỲ QUAN TRỌNG):
- NẾU dự án dùng Vite (có vite.config.js hoặc vite.config.ts), BẮT BUỘC phải viết lại file cấu hình đó, thêm thuộc tính `server: {{ allowedHosts: true }}` và `preview: {{ allowedHosts: true }}` vào cấu hình defineConfig.
- TUYỆT ĐỐI CẤM dùng `auto_patches` để thay đổi các file code logic chính (như server.ts, index.js, app.py, main.py). Cấm tuyệt đối việc sinh ra code mẫu (Hello World) làm mất logic gốc của người dùng. Hệ thống đã tự động vá lỗi IP rồi.

QUY ĐỊNH NIXPACKS_TOML (CỰC KỲ QUAN TRỌNG):
- TUYỆT ĐỐI KHÔNG tự tạo các block `[phases.setup]` hoặc `[phases.install]`. Hãy để Nixpacks tự nhận diện môi trường!
- CHỈ tạo block `[start]` chứa biến `cmd`.
- NẾU LÀ DỰ ÁN NODE.JS (React/Vite/Next): BẮT BUỘC thêm block:
  [variables]
  NIXPACKS_NODE_VERSION = "20"
  NIXPACKS_INSTALL_CMD = "npm install"
  CI = "false"
- NẾU LÀ DỰ ÁN PYTHON: TUYỆT ĐỐI KHÔNG thêm block `[variables]`. Hãy để trống phần này để Nixpacks tự xử lý pip và venv!

QUY ĐỊNH K3S YAML (CẤM TỰ CHẾ TÊN & CẤM DÙNG NGINX):
- `metadata.name` BẮT BUỘC là `{app_name}`.
- Deployment: BẮT BUỘC cấu hình `replicas: 1` dưới thẻ `spec:` (của deployment) để tiết kiệm tài nguyên.
- Deployment: BẮT BUỘC cấu hình `revisionHistoryLimit: 2` dưới thẻ `spec:` (của deployment) để giảm rác ổ cứng.
- Deployment: `image` BẮT BUỘC PHẢI LÀ: `{IMAGE_TAG}`.
- Deployment: BẮT BUỘC khối `imagePullSecrets: [{{name: regcred}}]` phải nằm trực tiếp dưới `spec.template.spec` (tức là ngang hàng với chữ `containers`).
- Ingress: Trỏ `{app_domain}` về Service `{app_name}`.
  + Dùng ĐÚNG 3 annotations của Traefik:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    traefik.ingress.kubernetes.io/router.entrypoints: "websecure"
    traefik.ingress.kubernetes.io/router.tls: "true"
  + BẮT BUỘC khối `tls` PHẢI ĐÚNG ĐỊNH DẠNG MẢNG CỦA YAML (Chú ý khoảng trắng và dấu gạch ngang -):
    spec:
      tls:
      - hosts:
        - {app_domain}
        secretName: {app_name}-tls
"""
            full_prompt = f"{system_prompt}\n\nĐây là mã nguồn:\n{code_context}"
            if build_error:
                full_prompt += f"\n\n[CẢNH BÁO TỪ HỆ THỐNG]: Lần Build trước thất bại với log:\n{build_error}\n\nHãy sửa nixpacks.toml hoặc mã nguồn cho đúng, BẮT BUỘC CHỈ TRẢ VỀ JSON!"

            try:
                config_json = run_agy_json(full_prompt, app_name=app_name, cwd=WORKSPACE_DIR, timeout=25)
            except Exception as e:
                write_log(app_name, f"⚠️ [LỖI AI/JSON] {e}. Đang yêu cầu AI thử lại...")
                build_error = f"Lỗi phân tích cú pháp JSON: {e}"
                if attempt == MAX_RETRIES:
                    raise Exception(f"AI liên tục sinh lỗi sau {MAX_RETRIES} lần thử: {e}")
                continue

            user_action = config_json.get("user_action_required", "").strip()
            if user_action:
                write_log(app_name, f"\n❌ [CẢNH BÁO TỪ HỆ THỐNG AI]\nPhát hiện lỗi mã nguồn cần bạn tự can thiệp:\n----------------------------------------\n{user_action}\n----------------------------------------\n-> VUI LÒNG SỬA VÀ TẢI LẠI FILE NÉN!")
                raise Exception("Hệ thống dừng lại để chờ người dùng kiểm tra mã nguồn.")

            patches = config_json.get("auto_patches", [])
            for patch in patches:
                f_name = patch.get("file_name")
                f_content = patch.get("new_content")
                if f_name and f_content:
                    write_log(app_name, f" 🛠️ [AUTO-HEAL] AI đang tự động vá lỗi file cấu hình: {f_name}...")
                    patch_path = os.path.join(WORKSPACE_DIR, f_name)
                    with open(patch_path, "w", encoding="utf-8") as pf:
                        pf.write(f_content)

            # Bước 4: Lưu cấu hình Nixpacks & K3s
            write_log(app_name, "[4] Đang lưu cấu hình K3s & Nixpacks...")
            with open(f"{WORKSPACE_DIR}/nixpacks.toml", "w", encoding="utf-8") as f:
                f.write(config_json.get("nixpacks_toml", ""))

            k8s_dir = os.path.join(WORKSPACE_DIR, "k8s")
            os.makedirs(k8s_dir, exist_ok=True)

            deployment_yaml = config_json.get("k3s_deployment", "")
            deployment_yaml = re.sub(r'image:\s*.*', f'image: {IMAGE_TAG}', deployment_yaml)

            with open(f"{k8s_dir}/deployment.yaml", "w", encoding="utf-8") as f:
                f.write(deployment_yaml)
            with open(f"{k8s_dir}/service.yaml", "w", encoding="utf-8") as f:
                f.write(config_json.get("k3s_service", ""))
            with open(f"{k8s_dir}/ingress.yaml", "w", encoding="utf-8") as f:
                f.write(config_json.get("k3s_ingress", ""))

            # Bước 5: Build Docker image bằng Nixpacks
            write_log(app_name, f"[5] Bắt đầu build Docker image ({IMAGE_TAG})...")
            try:
                run_cmd(["nixpacks", "build", ".", "--name", IMAGE_TAG], app_name, cwd=WORKSPACE_DIR)
                break
            except Exception as e:
                build_error = str(e)
                if attempt == MAX_RETRIES:
                    raise Exception(f"Đã nỗ lực 3 lần nhưng Build thất bại. Lỗi: {build_error}")
                write_log(app_name, f"\n[!] Build thất bại. Yêu cầu AI phân tích log lỗi để tìm hướng khắc phục...")

        # Bước 6: Đẩy Image lên Docker Hub
        write_log(app_name, f"[6] Đang đẩy Image lên Docker Hub ({DOCKER_USER})...")
        run_cmd(["docker", "push", IMAGE_TAG], app_name)

        # Bước 7: Áp dụng triển khai lên K3s
        write_log(app_name, "[7] Yêu cầu K3s triển khai ứng dụng (Kèm tên miền & SSL)...")
        run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/service.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/ingress.yaml"], app_name, cwd=WORKSPACE_DIR)

        write_log(app_name, f"=== [HOÀN TẤT] Ứng dụng từ file nén đã online tại https://{app_domain} ===")
        write_log(app_name, "DONE")

    except Exception as e:
        write_log(app_name, f"\n[LỖI QUY TRÌNH DEPLOY TỪ FILE NÉN] {e}")
        write_log(app_name, "ERROR")


# ==============================================================================
# 4. LUỒNG TRIỂN KHAI TỪ FILE NÉN KÈM THEO DATABASE (MYSQL, POSTGRES, MONGO...)
# ==============================================================================

def deploy_archive_with_db(
    archive_path: str,
    app_name: str,
    app_domain: str,
    db_type: str,
    backup_file_path: str = ""
):
    """
    Triển khai mã nguồn từ file nén kèm theo cấp phát Database K8s nội bộ.
    Tự động liên kết DATABASE_URL và seed database nếu có file backup sql/json.
    """
    from db_builder_core import (
        generate_db_password,
        get_db_strategy,
        generate_db_manifest,
        generate_standard_deployment,
        generate_standard_service,
        generate_standard_ingress,
        restore_database_backup,
        analyze_and_fix_backend,
    )

    write_log(app_name, f"\n=== [BẮT ĐẦU TRIỂN KHAI TỪ FILE NÉN CÓ DATABASE] {app_name} ({db_type.upper()}) ===")
    WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"
    os.makedirs(BASE_WORKSPACE, exist_ok=True)
    if os.path.exists(WORKSPACE_DIR):
        shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)

    try:
        # Bước 1: Giải nén mã nguồn
        extract_archive(archive_path, WORKSPACE_DIR, app_name)
        if os.path.exists(archive_path):
            try:
                os.remove(archive_path)
            except Exception:
                pass

        auto_heal_network_bindings(WORKSPACE_DIR, app_name)

        # Bước 2: Chuẩn bị mật khẩu và K3s Manifest cho Database
        write_log(app_name, f"[1] Đang chuẩn bị Database nội bộ ({db_type.upper()})...")
        pass_env_map = {
            "mysql": "MYSQL_PASSWORD",
            "mariadb": "MARIADB_PASSWORD",
            "postgresql": "POSTGRES_PASSWORD",
            "mongodb": "MONGO_INITDB_ROOT_PASSWORD",
        }
        db_password = ""
        target_env = pass_env_map.get(db_type.lower(), "MYSQL_PASSWORD")
        try:
            check_pass_cmd = (
                f"kubectl get statefulset {app_name}-db -n {K8S_NAMESPACE} "
                f"-o jsonpath='{{.spec.template.spec.containers[0].env[?(@.name==\"{target_env}\")].value}}'"
            )
            existing_pass = subprocess.check_output(check_pass_cmd, shell=True, text=True).strip()
            if existing_pass:
                db_password = existing_pass
        except Exception:
            pass

        if not db_password:
            db_password = generate_db_password()

        strategy = get_db_strategy(db_type, app_name, db_password, WORKSPACE_DIR)
        generate_db_manifest(app_name, db_password, WORKSPACE_DIR, strategy)

        # Bước 3: Triển khai Pod Database lên K3s
        write_log(app_name, f"[2] Đang triển khai Pod Database lên K3s...")
        run_cmd(["kubectl", "apply", "-f", "k8s/db.yaml"], app_name, cwd=WORKSPACE_DIR)

        write_log(app_name, "[3] Đang chờ Pod Database khởi động...")
        db_pod_name = ""
        while True:
            try:
                db_pod_name = subprocess.check_output(
                    f"kubectl get pods -n {K8S_NAMESPACE} -l app={app_name}-db -o jsonpath='{{.items[0].metadata.name}}'",
                    shell=True,
                    text=True,
                ).strip()
                status = subprocess.check_output(
                    f"kubectl get pod {db_pod_name} -n {K8S_NAMESPACE} -o jsonpath='{{.status.phase}}'",
                    shell=True,
                    text=True,
                ).strip()
                if status == "Running":
                    time.sleep(10)
                    break
            except Exception:
                pass
            time.sleep(3)

        write_log(app_name, f"✅ Database đã sẵn sàng: {db_pod_name}")

        # Khôi phục dữ liệu từ file backup (nếu có)
        if backup_file_path and os.path.exists(backup_file_path):
            write_log(app_name, f"[4] Đang nạp dữ liệu khởi tạo từ file backup...")
            restore_database_backup(db_pod_name, backup_file_path, db_type, db_password, strategy, app_name)

        # Bước 4: AI phân tích mã nguồn và inject DATABASE_URL vào code
        write_log(app_name, "[5] Đang cấu hình kết nối Database vào mã nguồn...")
        detected_port = analyze_and_fix_backend(
            WORKSPACE_DIR, app_name, app_domain, strategy["connection_info"]
        )

        # Bước 5: Build Image bằng Nixpacks
        IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
        write_log(app_name, f"[6] Bắt đầu build Docker image ({IMAGE_TAG})...")
        run_cmd(["nixpacks", "build", ".", "--name", IMAGE_TAG], app_name, cwd=WORKSPACE_DIR)

        # Bước 6: Push Image lên Docker Hub
        write_log(app_name, f"[7] Đang đẩy Image lên Docker Hub ({DOCKER_USER})...")
        run_cmd(["docker", "push", IMAGE_TAG], app_name)

        # Bước 7: Sinh Manifest và triển khai Web App
        write_log(app_name, "[8] Đang áp dụng Web App và Ingress SSL lên K3s...")
        generate_standard_deployment(app_name, IMAGE_TAG, detected_port, WORKSPACE_DIR)
        generate_standard_service(app_name, detected_port, WORKSPACE_DIR)
        generate_standard_ingress(app_name, app_domain, detected_port, WORKSPACE_DIR)

        run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/service.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/ingress.yaml"], app_name, cwd=WORKSPACE_DIR)

        write_log(app_name, f"=== [HOÀN TẤT] Ứng dụng kèm DB đã online tại https://{app_domain} ===")
        write_log(app_name, "DONE")

    except Exception as e:
        write_log(app_name, f"\n[LỖI QUY TRÌNH DEPLOY ARCHIVE + DB] {e}")
        write_log(app_name, "ERROR")
