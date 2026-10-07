
import os
import subprocess
import json
import requests
import shutil
import re
import time
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CẤU HÌNH HỆ THỐNG CỐ ĐỊNH
# ==========================================
K8S_NAMESPACE = "default"
DOCKER_USER = "tobi1008"
DOCKER_PASSWORD = os.getenv("DOCKER_PASSWORD")
BASE_WORKSPACE = "/opt/vibe-hosting/workspaces"
LOGS_DIR = "/opt/vibe-hosting/logs"
D1 = "127.0.0.1"
HERMES_API_URL = f"http://{D1}:8645/v1/chat/completions"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.getenv('INTERNAL_BEARER_TOKEN')}"
}

# DANH SÁCH AI MODEL DỰ PHÒNG MỚI NHẤT
FALLBACK_MODELS = [
    "stepfun/step-3.7-flash:free",
    "poolside/laguna-s-2.1:free",
    "meituan/longcat-2.5-preview:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "poolside/laguna-xs-2.1:free"
]

def write_log(app_name, message):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(f"{LOGS_DIR}/{app_name}.log", "a", encoding="utf-8") as f:
        f.write(message + "\n")
    print(message)

def run_cmd(cmd, app_name, cwd=None):
    try:
        write_log(app_name, f"> Chạy lệnh: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
        if result.stdout:
            write_log(app_name, result.stdout)
        return result.stdout
    except subprocess.CalledProcessError as e:
        error_msg = f"\n[LỖI LỆNH] {' '.join(cmd)}\nChi tiết lỗi: {e.stderr}"
        write_log(app_name, error_msg)
        raise Exception(error_msg)

def deploy_app(repo_url: str, app_name: str, app_domain: str):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(f"{LOGS_DIR}/{app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"=== [BẮT ĐẦU DEPLOY] {app_name} ===\n")

    IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
    WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"

    os.makedirs(BASE_WORKSPACE, exist_ok=True)
    if os.path.exists(WORKSPACE_DIR):
        shutil.rmtree(WORKSPACE_DIR)

    try:
        write_log(app_name, f"[1] Đang clone repo: {repo_url}...")
        run_cmd(["git", "clone", repo_url, WORKSPACE_DIR], app_name)

        # ==================================================
        # HỆ THỐNG PYTHON TỰ ĐỘNG VÁ LỖI IP (THAY THẾ AI LƯỜI BIẾNG)
        # ==================================================
        write_log(app_name, "[1.5] [AUTO-HEAL] Đang tự động quét và sửa lỗi IP nội bộ (localhost -> 0.0.0.0)...")
        for root, dirs, files in os.walk(WORKSPACE_DIR):
            if ".git" in root or "node_modules" in root or "venv" in root:
                continue
            for file in files:
                if file.endswith((".js", ".ts", ".py", ".json")):
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
        # ==================================================

        write_log(app_name, "[2] Đang quét cấu trúc mã nguồn...")
        tree_output = run_cmd(["tree", "-I", "node_modules|.git|venv", WORKSPACE_DIR], app_name)
        code_context = tree_output

        core_files = [
            "package.json", "requirements.txt", "composer.json", "go.mod",
            "app.py", "main.py", "index.js", "server.js",
            "vite.config.js", "vite.config.ts"
        ]

        for config_file in core_files:
            file_path = os.path.join(WORKSPACE_DIR, config_file)
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    code_context += f"\n\n--- Nội dung {config_file} ---\n" + f.read()

        MAX_RETRIES = 3
        build_error = ""

        for attempt in range(1, MAX_RETRIES + 1):
            write_log(app_name, f"\n[3] (Lần thử {attempt}/{MAX_RETRIES}) Bắt đầu gọi AI phân tích mã nguồn...")

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
            if build_error:
                system_prompt += f"\n\n[CẢNH BÁO TỪ HỆ THỐNG]: Lần Build trước thất bại với log:\n{build_error}\n\nHãy sửa nixpacks.toml hoặc mã nguồn cho đúng, CHỈ TRẢ VỀ JSON, KHÔNG DÙNG TOOL!"

            payload = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Đây là mã nguồn:\n{code_context}"}
                ]
            }

            api_success = False
            response = None

            for model_name in FALLBACK_MODELS:
                payload["model"] = model_name
                try:
                    write_log(app_name, f"⏳ Đang gửi yêu cầu tới Model: [{model_name}]...")
                    response = requests.post(HERMES_API_URL, headers=HEADERS, json=payload, timeout=150)

                    if response.status_code == 200:
                        api_success = True
                        write_log(app_name, f"✅ Model [{model_name}] phản hồi thành công!")
                        break
                    elif response.status_code == 429:
                        write_log(app_name, f"⚠️ Model [{model_name}] bị giới hạn Rate Limit (429). Chuyển sang model dự phòng...")
                        continue
                    else:
                        write_log(app_name, f"⚠️ Model [{model_name}] trả về lỗi {response.status_code}. Thử model tiếp theo...")
                        continue
                except Exception as e:
                    write_log(app_name, f"⚠️ Lỗi kết nối tới [{model_name}]: {str(e)}. Thử model tiếp theo...")
                    continue

            if not api_success or response is None:
                raise Exception("TẤT CẢ các AI Model trong danh sách dự phòng đều bị lỗi hoặc quá tải (429). Vui lòng thử lại sau!")

            # ==================================================
            # MÁY QUÉT X-QUANG ÉP LẤY JSON (ĐÃ VÁ LỖI CẤM SẬP)
            # ==================================================
            ai_raw_data = response.json()['choices'][0]['message']['content'].strip()

            json_match = re.search(r'\{.*\}', ai_raw_data, re.DOTALL)
            if json_match:
                ai_data = json_match.group(0)
            else:
                write_log(app_name, f"⚠️ Lỗi định dạng: AI đã cố gắng trả về Text hoặc gọi Tool. Đang yêu cầu AI thử lại...")
                build_error = f"LỖI NGHIÊM TRỌNG: Bạn vừa phản hồi bằng text hoặc dùng tool '{ai_raw_data[:50]}...'. BẮT BUỘC CHỈ TRẢ VỀ JSON."
                if attempt == MAX_RETRIES:
                    raise Exception("AI liên tục trả lời sai cấu trúc (không phải JSON) sau 3 lần thử.")
                continue

            try:
                config_json = json.loads(ai_data)
            except json.JSONDecodeError as e:
                write_log(app_name, f"⚠️ [LỖI JSON] Sai cú pháp JSON: {e}. Đang yêu cầu AI sửa lại...")
                build_error = "LỖI CÚ PHÁP JSON. Hãy kiểm tra lại dấu phẩy, dấu ngoặc kép và đảm bảo cấu trúc hợp lệ."
                if attempt == MAX_RETRIES:
                    raise Exception("AI liên tục sinh JSON sai cú pháp sau 3 lần thử.")
                continue

            user_action = config_json.get("user_action_required", "").strip()
            if user_action:
                write_log(app_name, f"\n❌ [CẢNH BÁO TỪ HỆ THỐNG AI]\nPhát hiện lỗi mã nguồn cần bạn tự can thiệp:\n----------------------------------------\n{user_action}\n----------------------------------------\n-> VUI LÒNG SỬA MÃ NGUỒN VÀ DEPLOY LẠI!")
                raise Exception("Hệ thống dừng lại để chờ người dùng sửa mã nguồn.")

            patches = config_json.get("auto_patches", [])
            for patch in patches:
                f_name = patch.get("file_name")
                f_content = patch.get("new_content")
                if f_name and f_content:
                    write_log(app_name, f" 🛠️ [AUTO-HEAL] AI đang tự động vá lỗi file cấu hình: {f_name}...")
                    patch_path = os.path.join(WORKSPACE_DIR, f_name)
                    with open(patch_path, "w", encoding="utf-8") as pf:
                        pf.write(f_content)

            write_log(app_name, "[4] Đang lưu cấu hình K3s & Nixpacks...")
            with open(f"{WORKSPACE_DIR}/nixpacks.toml", "w") as f:
                f.write(config_json.get("nixpacks_toml", ""))

            k8s_dir = os.path.join(WORKSPACE_DIR, "k8s")
            os.makedirs(k8s_dir, exist_ok=True)

            deployment_yaml = config_json.get("k3s_deployment", "")
            deployment_yaml = re.sub(r'image:\s*.*', f'image: {IMAGE_TAG}', deployment_yaml)

            with open(f"{k8s_dir}/deployment.yaml", "w") as f:
                f.write(deployment_yaml)
            with open(f"{k8s_dir}/service.yaml", "w") as f:
                f.write(config_json.get("k3s_service", ""))
            with open(f"{k8s_dir}/ingress.yaml", "w") as f:
                f.write(config_json.get("k3s_ingress", ""))

            write_log(app_name, f"[5] Bắt đầu build Docker image ({IMAGE_TAG})...")
            try:
                run_cmd(["nixpacks", "build", ".", "--name", IMAGE_TAG], app_name, cwd=WORKSPACE_DIR)
                break
            except Exception as e:
                build_error = str(e)
                if attempt == MAX_RETRIES:
                    raise Exception(f"Đã nỗ lực 3 lần nhưng Build thất bại. Lỗi: {build_error}")
                write_log(app_name, f"\n[!] Build thất bại. Yêu cầu AI phân tích log lỗi để tìm hướng khắc phục...")

        write_log(app_name, f"[6] Đang đẩy Image lên Docker Hub ({DOCKER_USER})...")
        run_cmd(["docker", "push", IMAGE_TAG], app_name)

        write_log(app_name, "[7] Yêu cầu K3s triển khai ứng dụng (Kèm tên miền & SSL)...")
        run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/service.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/ingress.yaml"], app_name, cwd=WORKSPACE_DIR)

        write_log(app_name, f"=== [HOÀN TẤT] Ứng dụng đã online tại https://{app_domain} ===")
        write_log(app_name, "DONE")

    except Exception as e:
        write_log(app_name, f"\n[LỖI QUY TRÌNH DEPLOY] {e}")
        write_log(app_name, "ERROR")

def delete_app(app_name: str):
    write_log(app_name, f"\n=== [BẮT ĐẦU XÓA DỰ ÁN] {app_name} ===")
    try:
        write_log(app_name, f"[1] Đang ra lệnh K3s xóa tài nguyên (Async)...")
        subprocess.run(["kubectl", "delete", "all,ingress,secret", "-l", f"app={app_name}", "--ignore-not-found=true", "--wait=false"], check=False)
        subprocess.run(["kubectl", "delete", "deployment,svc,ingress", app_name, "--ignore-not-found=true", "--wait=false"], check=False)
    except Exception as e:
        write_log(app_name, f"Lỗi phụ khi xóa K3s (bỏ qua): {e}")

    IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
    try:
        write_log(app_name, f"[2] Đang xóa Local Docker Image...")
        subprocess.run(["docker", "rmi", "-f", IMAGE_TAG], check=False, capture_output=True)
    except Exception: pass

    try:
        write_log(app_name, f"[3] Đang gọi API Docker Hub để xóa kho chứa...")
        p1, p2, p3 = "hub", "docker", "com"
        login_url = f"https://{p1}.{p2}.{p3}/v2/users/login/"
        login_resp = requests.post(login_url, json={"username": DOCKER_USER, "password": DOCKER_PASSWORD})
        if login_resp.status_code == 200:
            token = login_resp.json().get("token")
            requests.delete(f"https://{p1}.{p2}.{p3}/v2/repositories/{DOCKER_USER}/{app_name}/", headers={"Authorization": f"JWT {token}"})
    except Exception: pass

    try:
        write_log(app_name, f"[4] Đang dọn dẹp mã nguồn trên VPS...")
        shutil.rmtree(f"{BASE_WORKSPACE}/{app_name}", ignore_errors=True)
        write_log(app_name, f"=== [HOÀN TẤT XÓA] {app_name} ===")
        write_log(app_name, "DELETED")
    except Exception: pass
