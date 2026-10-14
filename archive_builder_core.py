import os
import shutil
import subprocess
import json
import re
import time
import zipfile
import tarfile
import traceback
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

from security import redact_secrets
from builder_core import (
    K8S_NAMESPACE,
    DOCKER_USER,
    DOCKER_PASSWORD,
    BASE_WORKSPACE,
    LOGS_DIR,
    run_cmd,
    run_agy_json,
    call_ai_json,
    delete_app,
)

UPLOAD_DIR = "/opt/vibe-hosting/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(BASE_WORKSPACE, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)


# ==============================================================================
# HÀM GHI LOG THỜI GIAN THỰC (REAL-TIME FLUSH & FSYNC)
# ==============================================================================

def write_log(app_name: str, message: str):
    """
    Ghi log nối tiếp (append) và ép xả buffer (fsync) ngay lập tức
    để API /api/logs/{app_name} trên Frontend stream theo thời gian thực.
    """
    message = redact_secrets(str(message))
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = f"{LOGS_DIR}/{app_name}.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(message + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
    print(message, flush=True)


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
    try:
        import rarfile
        with rarfile.RarFile(rar_path) as rf:
            rf.extractall(extract_dir)
            return
    except Exception:
        pass

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
        raise FileNotFoundError(f"Không tìm thấy file nén tải lên tại: {archive_path}")

    os.makedirs(extract_to, exist_ok=True)
    lower_path = archive_path.lower()

    write_log(app_name, f"[1] Đang tiến hành giải nén file: {os.path.basename(archive_path)}...")

    if lower_path.endswith(".zip"):
        safe_extract_zip(archive_path, extract_to)
    elif lower_path.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2", ".tar.xz", ".txz", ".tar")):
        safe_extract_tar(archive_path, extract_to)
    elif lower_path.endswith(".rar"):
        extract_rar(archive_path, extract_to, app_name)
    elif lower_path.endswith(".7z"):
        extract_7z(archive_path, extract_to, app_name)
    else:
        # Thử fallback qua 7z hoặc zipfile/tarfile
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

def deploy_archive_app(archive_path: str, app_name: str, app_domain: str, ai_provider: str = "agy"):
    """
    Quy trình triển khai hoàn chỉnh ứng dụng từ file nén tải lên (.zip, .tar, .rar, .7z)
    Tích hợp AI Engine (Antigravity hoặc Hermes) sinh cấu hình và tự phục hồi.
    Bọc toàn bộ task trong try...except và stream log thời gian thực.
    """
    try:
        write_log(app_name, f"\n=== [BẮT ĐẦU DEPLOY TỪ FILE NÉN] {app_name} (AI Engine: {ai_provider.upper()}) ===")

        IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
        WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"

        os.makedirs(BASE_WORKSPACE, exist_ok=True)
        if os.path.exists(WORKSPACE_DIR):
            shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)

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
        try:
            tree_output = run_cmd(["tree", "-L", "3", "-I", "node_modules|.git|venv|vendor|dist|build|target|wp-includes|wp-admin|.cache", WORKSPACE_DIR], app_name)
        except Exception:
            tree_output = "Không thể chạy lệnh tree. Tiếp tục phân tích các file cốt lõi."

        if len(tree_output) > 8000:
            tree_output = tree_output[:8000] + "\n... [Cắt bớt danh sách file vì quá dài]"
        code_context = tree_output

        core_files = [
            "package.json", "requirements.txt", "composer.json", "go.mod", "pom.xml",
            "app.py", "main.py", "index.js", "server.js",
            "vite.config.js", "vite.config.ts", "Dockerfile", "wp-config.php", "wp-config-sample.php"
        ]

        for config_file in core_files:
            file_path = os.path.join(WORKSPACE_DIR, config_file)
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        code_context += f"\n\n--- Nội dung {config_file} ---\n" + f.read(4000)
                except Exception:
                    pass

        # Giới hạn tổng dung lượng code_context tối đa 30KB
        if len(code_context) > 30000:
            code_context = code_context[:30000] + "\n... [Đã cắt ngắn bối cảnh mã nguồn]"

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

QUY ĐỊNH "user_action_required" (CỰC KỲ QUAN TRỌNG):
- "user_action_required" BẮT BUỘC PHẢI LÀ CHUỖI RỖNG "" nếu dự án có thể build được hoặc bạn đã sinh đủ cấu hình nixpacks_toml và k3s_deployment.
- TUYỆT ĐỐI KHÔNG giải thích những gì bạn đã làm, KHÔNG đưa ra lời khuyên hoặc hướng dẫn triển khai vào trường này.
- CHỈ ĐƯỢC điền vào "user_action_required" khi và chỉ khi mã nguồn bị lỗi nghiêm trọng không thể chạy được hoặc thiếu file cốt lõi. Nếu đã sinh đủ YAML, trường này BẮT BUỘC LÀ "".
"""
            full_prompt = f"{system_prompt}\n\nĐây là mã nguồn:\n{code_context}"
            if build_error:
                full_prompt += f"\n\n[CẢNH BÁO TỪ HỆ THỐNG]: Lần Build trước thất bại với log:\n{build_error}\n\nHãy sửa nixpacks.toml hoặc mã nguồn cho đúng, BẮT BUỘC CHỈ TRẢ VỀ JSON!"

            try:
                write_log(app_name, f"🤖 [AI Engine: {ai_provider.upper()}] Đang phân tích mã nguồn giải nén và sinh cấu hình...")
                config_json = call_ai_json(full_prompt, app_name=app_name, cwd=WORKSPACE_DIR, timeout=90, ai_provider=ai_provider)
            except Exception as e:
                write_log(app_name, f"⚠️ [LỖI AI/JSON ({ai_provider.upper()})] {e}. Đang yêu cầu AI thử lại...")
                build_error = f"Lỗi phân tích cú pháp JSON: {e}"
                if attempt == MAX_RETRIES:
                    raise Exception(f"AI ({ai_provider.upper()}) liên tục sinh lỗi sau {MAX_RETRIES} lần thử: {e}")
                continue

            user_action = config_json.get("user_action_required", "").strip()
            k3s_dep = config_json.get("k3s_deployment", "").strip()
            nix_toml = config_json.get("nixpacks_toml", "").strip()

            if user_action and (not k3s_dep or not nix_toml):
                write_log(app_name, f"\n❌ [CẢNH BÁO TỪ HỆ THỐNG AI]\nPhát hiện lỗi mã nguồn cần bạn tự can thiệp:\n----------------------------------------\n{user_action}\n----------------------------------------\n-> VUI LÒNG SỬA VÀ TẢI LẠI FILE NÉN!")
                raise Exception("Hệ thống dừng lại để chờ người dùng kiểm tra mã nguồn.")
            elif user_action:
                write_log(app_name, f"💡 [AI GHI CHÚ]: {user_action}")

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

        # Báo hiệu trạng thái hoàn tất cho Frontend
        write_log(app_name, f"\n=== HOÀN TẤT TRIỂN KHAI ===")
        write_log(app_name, f"=== [HOÀN TẤT] Ứng dụng từ file nén đã online tại https://{app_domain} ===")
        write_log(app_name, "DONE")

    except Exception as e:
        err_trace = traceback.format_exc()
        write_log(app_name, f"\n❌ [ERROR] Lỗi trong quy trình deploy file nén: {str(e)}")
        write_log(app_name, f"[CHI TIẾT TRACEBACK]:\n{err_trace}")
        write_log(app_name, "ERROR")


# ==============================================================================
# 4. HÀM NẠP BACKUP DATABASE BẰNG KUBECTL CP (CHỐNG DEADLOCK STDIN SPDY)
# ==============================================================================

def import_backup_to_db(db_type: str, db_pod_name: str, db_password: str, db_name: str, user: str, backup_file_path: str, app_name: str):
    """
    Nạp dữ liệu backup an toàn tuyệt đối:
    1. Kiểm tra cổng dịch vụ nội bộ (SELECT 1) thay vì sleep mù quáng.
    2. Sao chép file trực tiếp vào Pod qua 'kubectl cp'.
    3. Thực thi nạp file cục bộ bên trong Pod (< /tmp/file) để tránh lỗi deadlock stdin EOF của K3s.
    4. Giới hạn timeout để không bao giờ bị treo tiến trình ngầm.
    """
    if not backup_file_path or not os.path.exists(backup_file_path):
        return

    db_type_lower = db_type.lower()
    file_name = os.path.basename(backup_file_path)
    file_size_mb = os.path.getsize(backup_file_path) / (1024 * 1024)
    write_log(app_name, f"\n[DB Backup] Đang nạp dữ liệu từ file ({file_name} - {file_size_mb:.2f} MB) vào {db_type.upper()}...")

    # Bước A: Kiểm tra dịch vụ Database trong Pod đã thực sự sẵn sàng nhận kết nối
    write_log(app_name, f"[DB Backup] Đang kiểm tra cổng dịch vụ nội bộ của {db_type.upper()}...")
    ready = False
    for attempt in range(25):
        try:
            if db_type_lower in ["mysql", "mariadb"]:
                chk = subprocess.run(
                    f"kubectl exec {db_pod_name} -n {K8S_NAMESPACE} -- mysql -u {user} -p'{db_password}' -e 'SELECT 1;'",
                    shell=True, capture_output=True, text=True, timeout=10
                )
            elif db_type_lower == "postgresql":
                chk = subprocess.run(
                    f"kubectl exec {db_pod_name} -n {K8S_NAMESPACE} -- psql -U {user} -d {db_name} -c 'SELECT 1;'",
                    shell=True, capture_output=True, text=True, timeout=10
                )
            elif db_type_lower == "mongodb":
                chk = subprocess.run(
                    f"kubectl exec {db_pod_name} -n {K8S_NAMESPACE} -- mongosh --eval \"db.adminCommand('ping')\"",
                    shell=True, capture_output=True, text=True, timeout=10
                )
            else:
                ready = True
                break

            if chk.returncode == 0:
                ready = True
                write_log(app_name, f"✅ [DB Backup] Dịch vụ {db_type.upper()} đã sẵn sàng nhận kết nối!")
                break
        except Exception:
            pass
        time.sleep(3)

    if not ready:
        write_log(app_name, f"⚠️ [DB Backup] Pod chưa phản hồi truy vấn sau 75s, tiếp tục tiến hành sao chép file...")

    # Bước B: Sử dụng 'kubectl cp' để đưa file vào Pod rồi nạp tại chỗ
    temp_target_in_pod = f"/tmp/restore_{int(time.time())}.sql"
    clean_local_path = None

    try:
        if db_type_lower in ["mysql", "mariadb"]:
            write_log(app_name, "[DB Backup] Chuẩn hóa mã lệnh SQL (lọc bỏ USE/CREATE DATABASE)...")
            clean_local_path = f"/tmp/{app_name}_clean_restore_{int(time.time())}.sql"
            with open(backup_file_path, "r", encoding="utf-8", errors="ignore") as fin, \
                 open(clean_local_path, "w", encoding="utf-8") as fout:
                for line in fin:
                    stripped = line.strip().upper()
                    if stripped.startswith("CREATE DATABASE") or stripped.startswith("USE "):
                        continue
                    fout.write(line)

            source_file = clean_local_path
            write_log(app_name, f"[DB Backup] Đang sao chép file backup ({os.path.getsize(source_file) / (1024*1024):.2f} MB) vào {db_pod_name}...")
            cp_cmd = f"kubectl cp '{source_file}' {K8S_NAMESPACE}/{db_pod_name}:{temp_target_in_pod}"
            subprocess.run(cp_cmd, shell=True, check=True, timeout=180)

            write_log(app_name, f"[DB Backup] Đang thực thi nạp dữ liệu vào Database '{db_name}'...")
            exec_import = f"kubectl exec {db_pod_name} -n {K8S_NAMESPACE} -- /bin/sh -c \"mysql -u {user} -p'{db_password}' --force {db_name} < {temp_target_in_pod}\""
            subprocess.run(exec_import, shell=True, check=True, timeout=600)
            write_log(app_name, "   -> Nạp dữ liệu backup thành công 100%!")

        elif db_type_lower == "postgresql":
            write_log(app_name, f"[DB Backup] Đang sao chép file backup vào {db_pod_name}...")
            cp_cmd = f"kubectl cp '{backup_file_path}' {K8S_NAMESPACE}/{db_pod_name}:{temp_target_in_pod}"
            subprocess.run(cp_cmd, shell=True, check=True, timeout=180)

            write_log(app_name, f"[DB Backup] Đang thực thi nạp dữ liệu vào PostgreSQL '{db_name}'...")
            exec_import = f"kubectl exec {db_pod_name} -n {K8S_NAMESPACE} -- /bin/sh -c \"psql -U {user} -d {db_name} -f {temp_target_in_pod}\""
            subprocess.run(exec_import, shell=True, check=True, timeout=600)
            write_log(app_name, "   -> Nạp dữ liệu backup PostgreSQL thành công 100%!")

        elif db_type_lower == "mongodb":
            temp_dump = f"/tmp/dump_{int(time.time())}"
            write_log(app_name, f"[DB Backup] Đang sao chép file dump vào {db_pod_name}...")
            cp_cmd = f"kubectl cp '{backup_file_path}' {K8S_NAMESPACE}/{db_pod_name}:{temp_dump}"
            subprocess.run(cp_cmd, shell=True, check=True, timeout=180)

            write_log(app_name, f"[DB Backup] Đang thực thi mongorestore...")
            exec_import = f"kubectl exec {db_pod_name} -n {K8S_NAMESPACE} -- mongorestore --username root --password '{db_password}' --authenticationDatabase admin --archive={temp_dump} --gzip"
            subprocess.run(exec_import, shell=True, check=True, timeout=600)
            write_log(app_name, "   -> Nạp dữ liệu MongoDB thành công 100%!")

        else:
            write_log(app_name, f"   -> Chưa hỗ trợ nạp tự động cho loại DB '{db_type}'.")

    except subprocess.TimeoutExpired:
        write_log(app_name, f"   -> [CẢNH BÁO] Quá thời gian chờ (Timeout) khi nạp file backup. Hệ thống tiếp tục các bước deploy...")
    except Exception as e:
        write_log(app_name, f"   -> [CẢNH BÁO] Lỗi khi nạp file backup: {e}")
    finally:
        try:
            subprocess.run(f"kubectl exec {db_pod_name} -n {K8S_NAMESPACE} -- rm -f {temp_target_in_pod}", shell=True, capture_output=True, timeout=15)
        except Exception:
            pass
        if clean_local_path and os.path.exists(clean_local_path):
            try:
                os.remove(clean_local_path)
            except Exception:
                pass


# ==============================================================================
# 5. LUỒNG TRIỂN KHAI TỪ FILE NÉN KÈM THEO DATABASE (MYSQL, POSTGRES, MONGO...)
# ==============================================================================

def deploy_archive_with_db(
    archive_path: str,
    app_name: str,
    app_domain: str,
    db_type: str,
    backup_file_path: str = "",
    ai_provider: str = "agy"
):
    """
    Triển khai mã nguồn từ file nén kèm theo cấp phát Database K8s nội bộ.
    Tự động liên kết DATABASE_URL và seed database nếu có file backup sql/json.
    Bọc toàn bộ task trong try...except và stream log thời gian thực.
    """
    try:
        write_log(app_name, f"\n=== [BẮT ĐẦU TRIỂN KHAI TỪ FILE NÉN CÓ DATABASE] {app_name} ({db_type.upper()}) (AI Engine: {ai_provider.upper()}) ===")
        WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"
        os.makedirs(BASE_WORKSPACE, exist_ok=True)
        if os.path.exists(WORKSPACE_DIR):
            shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)

        # Bước 1: Giải nén mã nguồn
        extract_archive(archive_path, WORKSPACE_DIR, app_name)
        if os.path.exists(archive_path):
            try:
                os.remove(archive_path)
            except Exception:
                pass

        auto_heal_network_bindings(WORKSPACE_DIR, app_name)

        # Nạp an toàn các module K8s Database
        from db_builder_core import (
            generate_db_password,
            get_db_strategy,
            generate_db_manifest,
            generate_standard_deployment,
            generate_standard_service,
            generate_standard_ingress,
            analyze_and_fix_backend,
            auto_patch_nodejs_esm_conflicts,
        )

        # Bước 2: Chuẩn bị mật khẩu và K3s Manifest cho Database
        write_log(app_name, f"\n[1] Đang chuẩn bị Database nội bộ ({db_type.upper()})...")
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
        start_wait = time.time()
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

            if time.time() - start_wait > 300:
                raise Exception("Quá thời gian chờ khởi động Pod Database (Timeout 300s).")
            time.sleep(4)

        write_log(app_name, f"✅ Database đã sẵn sàng: {db_pod_name}")

        # Khôi phục dữ liệu từ file backup (nếu có)
        db_name = f"{app_name}_db".replace("-", "_")
        if backup_file_path and os.path.exists(backup_file_path):
            write_log(app_name, f"[4] Đang nạp dữ liệu khởi tạo từ file backup...")
            import_backup_to_db(
                db_type,
                db_pod_name,
                db_password,
                db_name,
                strategy["connection_info"]["user"],
                backup_file_path,
                app_name
            )

        db_info = strategy["connection_info"]
        db_info["password"] = db_password
        db_info["db_name"] = db_name

        # Bước 4: AI phân tích mã nguồn và inject DATABASE_URL vào code
        write_log(app_name, f"\n[5] Đang cấu hình kết nối Database vào mã nguồn (AI: {ai_provider.upper()})...")
        detected_port = analyze_and_fix_backend(
            WORKSPACE_DIR, app_name, app_domain, db_info, ai_provider=ai_provider
        )

        # Tự động vá ESM/CommonJS cho Node.js
        auto_patch_nodejs_esm_conflicts(WORKSPACE_DIR, app_name)

        # Bước 5: Build Image bằng Nixpacks
        IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
        write_log(app_name, f"\n[6] Bắt đầu build Docker image ({IMAGE_TAG})...")
        run_cmd(["nixpacks", "build", ".", "--name", IMAGE_TAG], app_name, cwd=WORKSPACE_DIR)

        # Bước 6: Push Image lên Docker Hub
        write_log(app_name, f"[7] Đang đẩy Image lên Docker Hub ({DOCKER_USER})...")
        run_cmd(["docker", "push", IMAGE_TAG], app_name)

        # Bước 7: Sinh Manifest và triển khai Web App
        write_log(app_name, "[8] Đang áp dụng Web App và Ingress SSL lên K3s...")
        k8s_dir = f"{WORKSPACE_DIR}/k8s"
        os.makedirs(k8s_dir, exist_ok=True)
        with open(f"{k8s_dir}/deployment.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_deployment(app_name, IMAGE_TAG, detected_port, db_info))
        with open(f"{k8s_dir}/service.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_service(app_name, detected_port))
        with open(f"{k8s_dir}/ingress.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_ingress(app_name, app_domain))

        run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/service.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/ingress.yaml"], app_name, cwd=WORKSPACE_DIR)

        # Báo hiệu trạng thái hoàn tất cho Frontend
        write_log(app_name, f"\n=== HOÀN TẤT TRIỂN KHAI ===")
        write_log(app_name, f"=== [HOÀN TẤT] Ứng dụng kèm DB đã online tại https://{app_domain} ===")
        write_log(app_name, "DONE")

    except Exception as e:
        err_trace = traceback.format_exc()
        write_log(app_name, f"\n❌ [ERROR] Lỗi trong quá trình triển khai Archive + DB: {str(e)}")
        write_log(app_name, f"[CHI TIẾT TRACEBACK]:\n{err_trace}")
        write_log(app_name, "ERROR")
