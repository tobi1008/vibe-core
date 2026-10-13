
import os
import subprocess
import json
import requests
import shutil
import re
import time
from typing import Optional, Dict, Any
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

# ==========================================
# CẤU HÌNH AI ENGINES: ANTIGRAVITY (AGY) & HERMES
# ==========================================
AGY_MODELS = [
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
]
FALLBACK_MODELS = AGY_MODELS

HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8645/v1/chat/completions")
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.getenv('INTERNAL_BEARER_TOKEN', '')}"
}
HERMES_MODELS = [
    "stepfun/step-3.7-flash:free",
    "poolside/laguna-s-2.1:free",
    "meituan/longcat-2.5-preview:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "nvidia/nemotron-3-ultra:free",
    "openrouter/free",
    "poolside/laguna-xs-2.1:free"
]

def get_agy_bin() -> str:
    """Tự động tìm kiếm đường dẫn tuyệt đối của binary agy trên máy chủ"""
    env_bin = os.getenv("AGY_BIN_PATH")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    
    which_bin = shutil.which("agy")
    if which_bin:
        return which_bin

    candidate_paths = [
        "/usr/local/bin/agy",
        "/usr/bin/agy",
        "/root/.local/bin/agy",
        "/root/.gemini/bin/agy",
        "/root/.gemini/antigravity-cli/bin/agy",
        os.path.expanduser("~/.local/bin/agy"),
        os.path.expanduser("~/.gemini/bin/agy"),
        os.path.expanduser("~/.gemini/antigravity-cli/bin/agy"),
    ]
    for p in candidate_paths:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return "agy"

def run_agy_prompt(prompt: str, app_name: str = "system", cwd: Optional[str] = None, timeout: int = 25, preferred_model: Optional[str] = None) -> str:
    """
    Thực thi prompt thông qua Antigravity CLI (agy) trực tiếp trên máy chủ.
    Tự động fallback giữa các phiên bản Gemini Flash.
    """
    models_to_try = [preferred_model] if preferred_model and preferred_model in AGY_MODELS else []
    for m in AGY_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    agy_bin = get_agy_bin()
    # Bổ sung PATH để service systemd luôn tìm thấy các thư mục bin phổ biến
    custom_env = os.environ.copy()
    extra_dirs = [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/root/.local/bin",
        "/root/.gemini/bin",
        "/root/.gemini/antigravity-cli/bin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.gemini/bin"),
    ]
    custom_env["PATH"] = ":".join(extra_dirs) + ":" + custom_env.get("PATH", "")

    # Bảo vệ chống lỗi Linux E2BIG / [Errno 7] Argument list too long
    safe_prompt = prompt
    if len(safe_prompt) > 70000:
        safe_prompt = safe_prompt[:70000] + "\n\n... [Nội dung prompt đã được rút gọn để đảm bảo an toàn bộ nhớ tham số Linux]"

    last_err = ""
    for model_name in models_to_try:
        try:
            write_log(app_name, f"🤖 [Antigravity AI] Đang gửi yêu cầu tới Model [{model_name}]...")
            cmd = [
                agy_bin,
                "-p", safe_prompt,
                "--model", model_name,
                "--dangerously-skip-permissions"
            ]
            res = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=custom_env,
                stdin=subprocess.DEVNULL
            )
            if res.returncode == 0 and res.stdout.strip():
                write_log(app_name, f"✅ [Antigravity AI] Model [{model_name}] phản hồi thành công!")
                return res.stdout.strip()
            else:
                err_detail = res.stderr.strip() or f"Exit code {res.returncode}"
                write_log(app_name, f"⚠️ Model [{model_name}] gặp sự cố ({err_detail}). Đang chuyển model dự phòng...")
                last_err = err_detail
        except subprocess.TimeoutExpired:
            write_log(app_name, f"⚠️ Model [{model_name}] quá thời gian chờ ({timeout}s). Chuyển model dự phòng...")
            last_err = f"Timeout {timeout}s"
        except Exception as e:
            write_log(app_name, f"⚠️ Lỗi thực thi agy ({agy_bin}) với [{model_name}]: {str(e)}")
            last_err = str(e)

    raise Exception(f"Tất cả các Model Antigravity đều không phản hồi: {last_err}")

def run_agy_json(prompt: str, app_name: str = "system", cwd: Optional[str] = None, timeout: int = 25, preferred_model: Optional[str] = None) -> dict:
    """
    Gọi agy và bóc tách dữ liệu JSON an toàn từ kết quả trả về.
    """
    raw_output = run_agy_prompt(prompt, app_name=app_name, cwd=cwd, timeout=timeout, preferred_model=preferred_model)
    # Loại bỏ mã màu ANSI từ terminal nếu CLI xuất ra
    clean_text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw_output)

    # Thử bóc tách khối ```json ... ```
    md_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', clean_text, re.DOTALL)
    if md_match:
        clean_json = md_match.group(1).strip()
    else:
        # Tìm khối { ... } ngoài cùng
        start_idx = clean_text.find('{')
        end_idx = clean_text.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            clean_json = clean_text[start_idx:end_idx+1].strip()
        else:
            clean_json = clean_text.strip()
            clean_json = re.sub(r"^`{1,3}(?:json)?\s*", "", clean_json, flags=re.IGNORECASE)
            clean_json = re.sub(r"\s*`{1,3}$", "", clean_json)

    clean_json = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', clean_json)
    try:
        return json.loads(clean_json)
    except Exception as e:
        write_log(app_name, f"⚠️ [LỖI JSON AGY] Không thể parse JSON từ output của agy: {e}\nRaw Output: {raw_output[:300]}...")
        raise e

def run_hermes_prompt(prompt: str, app_name: str = "system", timeout: int = 90, preferred_model: Optional[str] = None) -> str:
    """
    Thực thi prompt thông qua Hermes API gateway (OpenAI-compatible HTTP endpoint).
    Tự động fallback giữa các mô hình trong HERMES_MODELS.
    """
    models_to_try = [preferred_model] if preferred_model and preferred_model in HERMES_MODELS else []
    for m in HERMES_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    payload = {
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    last_err = ""
    for model_name in models_to_try:
        payload["model"] = model_name
        try:
            write_log(app_name, f"🤖 [Hermes AI] Đang gửi yêu cầu tới Model [{model_name}]...")
            resp = requests.post(HERMES_API_URL, headers=HEADERS, json=payload, timeout=timeout)
            if resp.status_code == 200:
                write_log(app_name, f"✅ [Hermes AI] Model [{model_name}] phản hồi thành công!")
                return resp.json()['choices'][0]['message']['content'].strip()
            elif resp.status_code == 429:
                write_log(app_name, f"⚠️ Model [{model_name}] bị Rate Limit (429). Chuyển model dự phòng...")
                last_err = f"Rate Limit (429) trên {model_name}"
                continue
            else:
                err_text = resp.text[:120]
                write_log(app_name, f"⚠️ Model [{model_name}] trả về lỗi {resp.status_code} ({err_text}). Thử model tiếp theo...")
                last_err = f"HTTP {resp.status_code}: {err_text}"
                continue
        except subprocess.TimeoutExpired:
            write_log(app_name, f"⚠️ Model [{model_name}] quá thời gian chờ ({timeout}s). Chuyển model dự phòng...")
            last_err = f"Timeout {timeout}s"
            continue
        except Exception as e:
            write_log(app_name, f"⚠️ Lỗi kết nối Hermes [{model_name}]: {str(e)}. Thử model tiếp theo...")
            last_err = str(e)
            continue

    raise Exception(f"Tất cả các Model Hermes đều không phản hồi: {last_err}")

def run_hermes_json(prompt: str, app_name: str = "system", timeout: int = 90, preferred_model: Optional[str] = None) -> dict:
    """
    Gọi Hermes API và bóc tách dữ liệu JSON an toàn từ kết quả trả về.
    """
    raw_output = run_hermes_prompt(prompt, app_name=app_name, timeout=timeout, preferred_model=preferred_model)
    json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
    if json_match:
        clean_json = json_match.group(0)
    else:
        clean_json = raw_output.strip()
        clean_json = re.sub(r"^`{1,3}(?:json)?\s*", "", clean_json, flags=re.IGNORECASE)
        clean_json = re.sub(r"\s*`{1,3}$", "", clean_json)

    clean_json = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', clean_json)
    return json.loads(clean_json)

def call_ai_json(
    prompt: str,
    app_name: str = "system",
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    preferred_model: Optional[str] = None,
    ai_provider: str = "agy"
) -> dict:
    """
    Bộ điều phối AI Engine hợp nhất:
    - 'agy': Google Antigravity CLI (Gemini Flash)
    - 'hermes': Hermes SRE Agent (Open-source LLMs qua API Gateway)
    """
    provider = (ai_provider or "agy").lower().strip()
    if provider == "hermes":
        to = timeout or 90
        return run_hermes_json(prompt, app_name=app_name, timeout=to, preferred_model=preferred_model)
    else:
        to = timeout if (timeout and timeout <= 45) else 35
        return run_agy_json(prompt, app_name=app_name, cwd=cwd, timeout=to, preferred_model=preferred_model)

def call_ai_prompt(
    prompt: str,
    app_name: str = "system",
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    preferred_model: Optional[str] = None,
    ai_provider: str = "agy"
) -> str:
    """
    Bộ điều phối AI dạng Text/Chat tự do:
    - 'agy': Google Antigravity CLI
    - 'hermes': Hermes SRE Agent
    """
    provider = (ai_provider or "agy").lower().strip()
    if provider == "hermes":
        to = timeout or 90
        return run_hermes_prompt(prompt, app_name=app_name, timeout=to, preferred_model=preferred_model)
    else:
        to = timeout if (timeout and timeout <= 45) else 35
        return run_agy_prompt(prompt, app_name=app_name, cwd=cwd, timeout=to, preferred_model=preferred_model)

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

def deploy_app(repo_url: str, app_name: str, app_domain: str, ai_provider: str = "agy"):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(f"{LOGS_DIR}/{app_name}.log", "w", encoding="utf-8") as f:
        f.write(f"=== [BẮT ĐẦU DEPLOY] {app_name} (AI Engine: {ai_provider.upper()}) ===\n")

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
            "vite.config.js", "vite.config.ts", "wp-config.php", "wp-config-sample.php"
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

QUY ĐỊNH "user_action_required" (CỰC KỲ QUAN TRỌNG):
- "user_action_required" BẮT BUỘC PHẢI LÀ CHUỖI RỖNG "" nếu bạn có thể sinh ra cấu hình nixpacks_toml và k3s_deployment.
- TUYỆT ĐỐI KHÔNG giải thích những gì bạn đã làm, KHÔNG đưa ra lời khuyên hoặc hướng dẫn triển khai vào trường này.
- CHỈ ĐƯỢC điền vào "user_action_required" khi và chỉ khi mã nguồn bị lỗi nghiêm trọng không thể chạy được hoặc thiếu file cốt lõi. Nếu đã sinh đủ YAML, trường này BẮT BUỘC LÀ "".
"""
            full_prompt = f"{system_prompt}\n\nĐây là mã nguồn:\n{code_context}"
            if build_error:
                full_prompt += f"\n\n[CẢNH BÁO TỪ HỆ THỐNG]: Lần Build trước thất bại với log:\n{build_error}\n\nHãy sửa nixpacks.toml hoặc mã nguồn cho đúng, BẮT BUỘC CHỈ TRẢ VỀ JSON!"

            try:
                write_log(app_name, f"🤖 [AI Engine: {ai_provider.upper()}] Đang phân tích mã nguồn và sinh cấu hình...")
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
                write_log(app_name, f"\n❌ [CẢNH BÁO TỪ HỆ THỐNG AI]\nPhát hiện lỗi mã nguồn cần bạn tự can thiệp:\n----------------------------------------\n{user_action}\n----------------------------------------\n-> VUI LÒNG SỬA MÃ NGUỒN VÀ DEPLOY LẠI!")
                raise Exception("Hệ thống dừng lại để chờ người dùng sửa mã nguồn.")
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
