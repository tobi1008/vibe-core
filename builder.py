import os
import subprocess
import json
import requests
import shutil

# ==========================================
# CẤU HÌNH HỆ THỐNG VIBE HOSTING
# ==========================================
REPO_URL = "https://github.com/tobi1008/vibecode.git" 
APP_NAME = "vibecode"
DOCKER_USER = "tobi1008" 
IMAGE_TAG = f"{DOCKER_USER}/{APP_NAME}:latest"

# === CẤU HÌNH TÊN MIỀN ===
APP_DOMAIN = f"{APP_NAME}.vibe.quyenlt.com"

AI_MODEL = "stepfun/step-3.7-flash:free" 
BASE_WORKSPACE = "/opt/vibe-hosting/workspaces"
WORKSPACE_DIR = f"{BASE_WORKSPACE}/{APP_NAME}"
HERMES_API_URL = "http://127.0.0.1:8645/v1/chat/completions"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer vibe-token" 
}

# ==========================================
# HÀM HỖ TRỢ
# ==========================================
def run_cmd(cmd, cwd=None):
    try:
        result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"\n[LỖI LỆNH] {' '.join(cmd)}")
        print(f"Chi tiết lỗi: {e.stderr}")
        exit(1)

# ==========================================
# LUỒNG XỬ LÝ CHÍNH
# ==========================================
print("=== VIBE HOSTING: KHỞI TẠO LUỒNG DEPLOY ===")

os.makedirs(BASE_WORKSPACE, exist_ok=True)
if os.path.exists(WORKSPACE_DIR):
    shutil.rmtree(WORKSPACE_DIR)

print(f"\n[1] Đang clone repo: {REPO_URL}...")
run_cmd(["git", "clone", REPO_URL, WORKSPACE_DIR])

print("[2] Đang quét cấu trúc mã nguồn...")
tree_output = run_cmd(["tree", "-I", "node_modules|.git|venv", WORKSPACE_DIR])
code_context = tree_output

for config_file in ["package.json", "requirements.txt", "composer.json", "go.mod"]:
    file_path = os.path.join(WORKSPACE_DIR, config_file)
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            code_context += f"\n\n--- Nội dung {config_file} ---\n" + f.read()

print("[3] Đang gọi Hermes Agent phân tích và sinh cấu hình (Nixpacks + K3s)...")
system_prompt = f"""
Bạn là AI DevOps Agent cho nền tảng Vibe Hosting.
Nhiệm vụ: Đọc file package.json/mã nguồn để sinh ra cấu hình nixpacks.toml và các file K3s YAML.
You must respond with valid JSON only. No markdown code blocks, no explanation, just pure JSON.

BẮT BUỘC trả về JSON theo cấu trúc chính xác sau:
{{
  "nixpacks_toml": "nội dung file nixpacks.toml (CHÚ Ý QUAN TRỌNG: Nếu dự án dùng Node.js/Next.js, BẮT BUỘC khai báo block [variables] và thêm biến NIXPACKS_NODE_VERSION = '20'. Tuyệt đối không dùng block [providers]).",
  "k3s_deployment": "nội dung file deployment.yaml dạng string",
  "k3s_service": "nội dung file service.yaml dạng string",
  "k3s_ingress": "nội dung file ingress.yaml dạng string"
}}

YÊU CẦU CHUYÊN MÔN CHO K3S:
- Deployment: imagePullSecrets: [{{name: regcred}}], tên image: {IMAGE_TAG}, tên ứng dụng: {APP_NAME}.
- Service: Tự nhận diện Port từ mã nguồn.
- Ingress: 
  + Sử dụng apiVersion networking.k8s.io/v1. Định tuyến {APP_DOMAIN} trỏ về Service {APP_NAME}.
  + BẮT BUỘC thêm annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    traefik.ingress.kubernetes.io/router.entrypoints: "websecure"
    traefik.ingress.kubernetes.io/router.tls: "true"
  + BẮT BUỘC thêm block `tls` cấu hình hosts là {APP_DOMAIN} và secretName là {APP_NAME}-tls.
"""

payload = {
    "model": AI_MODEL,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Đây là mã nguồn:\n{code_context}"}
    ]
}

try:
    response = requests.post(HERMES_API_URL, headers=HEADERS, json=payload)
    response.raise_for_status()
    
    ai_data = response.json()['choices'][0]['message']['content']
    
    ai_data = ai_data.strip()
    if ai_data.startswith("```"):
        ai_data = ai_data.strip("`").replace("json\n", "", 1).strip()
        
    config_json = json.loads(ai_data)
except Exception as e:
    print(f"\n[LỖI AI] Không thể kết nối hoặc phân tích từ Hermes API: {e}")
    if 'ai_data' in locals():
        print(f"Dữ liệu thô AI trả về gây lỗi:\n{ai_data}")
    exit(1)

print("[4] Lưu cấu hình do AI sinh ra (nixpacks.toml & YAML)...")

with open(f"{WORKSPACE_DIR}/nixpacks.toml", "w") as f:
    f.write(config_json.get("nixpacks_toml", ""))

k8s_dir = os.path.join(WORKSPACE_DIR, "k8s")
os.makedirs(k8s_dir, exist_ok=True)
with open(f"{k8s_dir}/deployment.yaml", "w") as f:
    f.write(config_json.get("k3s_deployment", ""))
with open(f"{k8s_dir}/service.yaml", "w") as f:
    f.write(config_json.get("k3s_service", ""))
with open(f"{k8s_dir}/ingress.yaml", "w") as f:
    f.write(config_json.get("k3s_ingress", ""))

print(f"\n[5] Bắt đầu build Nixpacks image: {IMAGE_TAG}...")
subprocess.run(["nixpacks", "build", ".", "--name", IMAGE_TAG], cwd=WORKSPACE_DIR, check=True)

print(f"\n[6] Đang đẩy Image lên Docker Hub ({DOCKER_USER})...")
subprocess.run(["docker", "push", IMAGE_TAG], check=True)

print("\n[7] Đang yêu cầu K3s (VPS 2) triển khai ứng dụng (Kèm tên miền & SSL)...")
run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], cwd=WORKSPACE_DIR)
run_cmd(["kubectl", "apply", "-f", "k8s/service.yaml"], cwd=WORKSPACE_DIR)
run_cmd(["kubectl", "apply", "-f", "k8s/ingress.yaml"], cwd=WORKSPACE_DIR)

print(f"\n=== HOÀN TẤT! ỨNG DỤNG ĐÃ ĐƯỢC ĐƯA LÊN MẠNG ===")
print(f"🌍 Tên miền truy cập: https://{APP_DOMAIN}")
