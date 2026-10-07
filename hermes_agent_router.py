import os
import json
import re
import subprocess
import requests
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from builder_core import (
    BASE_WORKSPACE,
    K8S_NAMESPACE,
    LOGS_DIR,
    HERMES_API_URL,
    HEADERS,
    FALLBACK_MODELS,
    write_log,
)
from cicd_core import redeploy_app

router = APIRouter(prefix="/api/ai-debug", tags=["Hermes Agent - AI Debug"])

# BẢNG TRỌNG SỐ HÀNH ĐỘNG: ƯU TIÊN HẠ TẦNG K3S TRƯỚC, SỬA CODE LÀ BIỆN PHÁP CUỐI CÙNG
ACTION_PRIORITY = {
    "set_env": 1,            # ⚡ ƯU TIÊN 1: Bơm biến môi trường K8s (5 giây, không cần build lại image)
    "apply_manifest": 2,     # ⚡ ƯU TIÊN 2: Áp dụng manifest K8s (Middleware, Ingress, Secret) tại host VPS
    "restart_deployment": 3, # ⚡ ƯU TIÊN 3: Khởi động lại Pod nhận cấu hình mới
    "exec_pod": 4,           # ⚡ ƯU TIÊN 4: Chạy seed/migrate DB trong Pod đang sống (CHỈ LỆNH APP, KHÔNG CHẠY KUBECTL)
    "edit_file": 5,          # ⚠️ BIỆN PHÁP CUỐI CÙNG: Chỉ dùng khi code dính lỗi cú pháp không chạy được
    "rebuild": 6,            # Đi kèm với edit_file
}

class RemediationAction(BaseModel):
    action_type: str
    target: Optional[str] = None
    path: Optional[str] = None
    file_path: Optional[str] = None
    content: Optional[str] = None
    diff: Optional[str] = None
    command: Optional[str] = None
    manifest: Optional[str] = None
    env_vars: Optional[Dict[str, str]] = None
    description: str

class AIDebugRequest(BaseModel):
    app_name: str
    component: Optional[str] = "be"
    user_note: Optional[str] = None
    auto_apply: bool = False
    preferred_model: Optional[str] = None
    github_token: Optional[str] = None
    auto_push: Optional[bool] = True

class ExecuteActionsPayload(BaseModel):
    actions: List[RemediationAction]
    github_token: Optional[str] = None
    auto_push: Optional[bool] = True

# Thêm Schema cho tính năng Chat liên tục
class ChatMessage(BaseModel):
    role: str  # "user" hoặc "assistant"
    content: str

class AIChatRequest(BaseModel):
    app_name: str
    component: Optional[str] = "be"
    message: str
    history: List[ChatMessage] = []
    preferred_model: Optional[str] = None
    
def detect_action_component(action: RemediationAction, default_comp: str = "be") -> str:
    """Tự động nhận diện chính xác component (fe hoặc be) từ target hoặc file path"""
    raw_target = (action.target or "").lower().strip()
    raw_path = (action.path or action.file_path or "").lower().strip()

    # Nhận diện qua tên Target
    if any(raw_target.endswith(x) for x in ["-fe", "_fe", "/fe"]) or raw_target in ["fe", "frontend", "client", "web", "ui"]:
        return "fe"
    if any(raw_target.endswith(x) for x in ["-be", "_be", "/be"]) or raw_target in ["be", "backend", "server", "api"]:
        return "be"

    # Nhận diện qua tiền tố đường dẫn file
    if any(raw_path.startswith(p) for p in ["frontend/", "fe/", "client/", "web/"]):
        return "fe"
    if any(raw_path.startswith(p) for p in ["backend/", "be/", "server/", "api/"]):
        return "be"

    # Nhận diện qua các file đặc thù Frontend
    fe_files = [
        "vite.config.js", "vite.config.ts", "next.config.js", "next.config.mjs",
        "next.config.ts", "tailwind.config.js", "tailwind.config.ts", "index.html"
    ]
    if any(raw_path.endswith(f) for f in fe_files):
        return "fe"

    return default_comp


def find_actual_k8s_resource(resource_type: str, candidate_name: str, app_name: str) -> Optional[str]:
    """
    Tự động dò tìm tài nguyên thực tế trên K3s để triệt tiêu lỗi NotFound:
    - Khắc phục tình trạng hoán đổi tên (ví dụ: '3tshop-be' vs 'shop3t-be').
    - Khớp theo thành phần tên và host domain thực tế.
    """
    try:
        cmd = ["kubectl", "get", resource_type, "-n", K8S_NAMESPACE, "-o", "jsonpath={.items[*].metadata.name}"]
        items = subprocess.check_output(cmd, text=True, timeout=5).strip().split()
        if not items:
            return None
        if candidate_name in items:
            return candidate_name

        cand_clean = re.sub(r"[^a-zA-Z0-9]", "", candidate_name).lower()
        cand_tokens = set(re.findall(r"[a-zA-Z]+|\d+", candidate_name.lower()))
        is_be = "be" in candidate_name.lower() or "backend" in candidate_name.lower()
        is_fe = "fe" in candidate_name.lower() or "frontend" in candidate_name.lower()

        best_match = None
        best_score = -1

        for item in items:
            item_lower = item.lower()
            if is_be and "be" not in item_lower and "backend" not in item_lower:
                continue
            if is_fe and "fe" not in item_lower and "frontend" not in item_lower:
                continue

            item_tokens = set(re.findall(r"[a-zA-Z]+|\d+", item_lower))
            overlap = len(cand_tokens.intersection(item_tokens))

            # So khớp chuỗi loại bỏ ký tự đặc biệt (3tshop <-> shop3t)
            item_clean = re.sub(r"[^a-zA-Z0-9]", "", item_lower)
            if sorted(cand_clean) == sorted(item_clean):
                overlap += 10

            if overlap > best_score:
                best_score = overlap
                best_match = item

        if best_match and best_score >= 1:
            return best_match

        # Fallback: Nếu chỉ có duy nhất 1 Ingress/Deployment BE hoặc FE
        if is_be:
            be_candidates = [i for i in items if "be" in i.lower()]
            if len(be_candidates) == 1:
                return be_candidates[0]
        elif is_fe:
            fe_candidates = [i for i in items if "fe" in i.lower()]
            if len(fe_candidates) == 1:
                return fe_candidates[0]
    except Exception:
        pass
    return None


def sanitize_k8s_target(raw_target: str, resource_type: str = "deployment", app_name: str = "") -> str:
    """
    Làm sạch tên tài nguyên K8s:
    - Loại bỏ các tiền tố như 'deployment/', 'deploy/', 'ingress/', 'svc/', 'pod/'...
      để triệt tiêu hoàn toàn lỗi 'arguments in resource/name form may not have more than one slash'.
    - Tự động đối chiếu với K8s qua find_actual_k8s_resource để bắt đúng tên (ví dụ: 'shop3t-be' thay vì '3tshop-be').
    """
    if not raw_target or not raw_target.strip():
        raw_target = f"{app_name}-be" if app_name else "app"

    # Loại bỏ tiền tố loại tài nguyên nếu có
    clean_target = re.sub(
        r"^(?:deployments?|deploy|ingresses?|ing|services?|svc|pods?|pod)/",
        "",
        raw_target.strip(),
        flags=re.IGNORECASE
    )

    # Thử đối chiếu tên thực tế đang chạy trên K8s
    if app_name:
        real_name = find_actual_k8s_resource(resource_type, clean_target, app_name)
        if real_name:
            return real_name

    return clean_target


def resolve_workspace(app_name: str, component: str = "be") -> Optional[str]:
    candidate_paths = []
    if component == "fe":
        candidate_paths.extend([
            os.path.join(BASE_WORKSPACE, f"{app_name}-fe"),
            os.path.join("/opt/vibe-hosting/workspaces", f"{app_name}-fe"),
        ])
    else:
        candidate_paths.extend([
            os.path.join(BASE_WORKSPACE, f"{app_name}-be"),
            os.path.join("/opt/vibe-hosting/workspaces", f"{app_name}-be"),
        ])

    candidate_paths.extend([
        os.path.join(BASE_WORKSPACE, f"{app_name}-monorepo"),
        os.path.join(BASE_WORKSPACE, app_name),
        os.path.join("/opt/vibe-hosting/workspaces", f"{app_name}-monorepo"),
        os.path.join("/opt/vibe-hosting/workspaces", app_name),
        os.path.join("/opt/vibe-hosting/workspace", f"{app_name}-monorepo"),
        os.path.join("/opt/vibe-hosting/workspace", app_name),
    ])

    for p in candidate_paths:
        if os.path.exists(p) and os.path.isdir(p):
            return os.path.abspath(p)
    return None


def extract_culprit_files_from_logs(logs_text: str, ws: str) -> Dict[str, str]:
    """Tự động bóc tách các file xuất hiện trong Stack Trace lỗi và đọc nội dung file đó"""
    culprit_files = {}
    if not logs_text or not ws or not os.path.isdir(ws):
        return culprit_files

    patterns = [
        r"(?:/app/|/src/|[a-zA-Z0-9_-]+/)((?:src|app|utils|controllers|services|routes|models|config)/[a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)",
        r"(?:at\s+.*\()((?:src|app|utils|controllers|services|routes|models|config)/[a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+):[0-9]+:[0-9]+",
        r"\s+at\s+.*[/\\]((?:src|app|utils|controllers|services|routes|models|config)[/\\][a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]+)",
    ]

    matched_rel_paths = set()
    for pat in patterns:
        for m in re.finditer(pat, logs_text):
            clean_p = m.group(1).replace("\\", "/").strip()
            if not any(clean_p.endswith(ext) for ext in [".js", ".ts", ".jsx", ".tsx", ".json", ".mjs"]):
                continue
            matched_rel_paths.add(clean_p)

    for rel in matched_rel_paths:
        candidates = [
            os.path.join(ws, rel),
            os.path.join(ws, "src", rel) if not rel.startswith("src/") else None,
        ]
        for cand in candidates:
            if cand and os.path.exists(cand) and os.path.isfile(cand):
                try:
                    with open(cand, "r", encoding="utf-8") as fp:
                        actual_rel = os.path.relpath(cand, ws).replace("\\", "/")
                        culprit_files[actual_rel] = fp.read(8000)
                    break
                except Exception:
                    pass

    return culprit_files


def collect_debug_context(app_name: str, component: str = "be") -> Dict[str, Any]:
    context: Dict[str, Any] = {
        "app_name": app_name,
        "component": component,
        "build_logs": "",
        "pod_runtime_logs": "",
        "k8s_events": "",
        "k8s_ingresses": "",
        "k8s_services": "",
        "key_files": {},
    }

    build_log_candidates = [
        f"{LOGS_DIR}/{app_name}.log",
        f"/opt/vibe-hosting/logs/{app_name}.log",
    ]
    for log_path in build_log_candidates:
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    context["build_logs"] = "".join(lines[-250:])
                break
            except Exception:
                pass

    target_deploy = f"{app_name}-{component}" if component in ["be", "fe"] else app_name
    try:
        check_cmd = (
            f"kubectl get pods -n {K8S_NAMESPACE} "
            f"-l 'app in ({target_deploy}, {app_name})' "
            f"-o jsonpath='{{.items[*].metadata.name}}'"
        )
        pod_names = subprocess.check_output(check_cmd, shell=True, text=True).strip().split()
        if pod_names:
            target_pod = pod_names[0]
            res = subprocess.run(
                ["kubectl", "logs", target_pod, "-n", K8S_NAMESPACE, "--tail=180"],
                capture_output=True, text=True
            )
            context["pod_runtime_logs"] = res.stdout or res.stderr
            if not res.stdout:
                res_prev = subprocess.run(
                    ["kubectl", "logs", target_pod, "-n", K8S_NAMESPACE, "--previous", "--tail=100"],
                    capture_output=True, text=True
                )
                if res_prev.stdout:
                    context["pod_runtime_logs"] += f"\n[CRASH PREVIOUS LOGS]:\n{res_prev.stdout}"
    except Exception as e:
        context["pod_runtime_logs"] = f"Không thể lấy log container: {str(e)}"

    try:
        ing_res = subprocess.run(
            ["kubectl", "get", "ingress", "-n", K8S_NAMESPACE, "-o", "wide"],
            capture_output=True, text=True
        )
        context["k8s_ingresses"] = ing_res.stdout.strip()
    except Exception:
        pass

    try:
        svc_res = subprocess.run(
            ["kubectl", "get", "svc", "-n", K8S_NAMESPACE],
            capture_output=True, text=True
        )
        context["k8s_services"] = svc_res.stdout.strip()
    except Exception:
        pass

    try:
        events_res = subprocess.run(
            ["kubectl", "get", "events", "-n", K8S_NAMESPACE, "--sort-by=.metadata.creationTimestamp"],
            capture_output=True, text=True
        )
        context["k8s_events"] = "\n".join(events_res.stdout.splitlines()[-25:])
    except Exception:
        pass

    ws = resolve_workspace(app_name, component)
    if ws:
        tracked_filenames = [
            "package.json", "schema.prisma", "prisma.config.ts",
            "next.config.js", "next.config.mjs", "next.config.ts",
            "vite.config.js", "vite.config.ts",
            "api.js", "api.ts", "endpoints.js", "client.js",
            ".env", ".env.production", ".env.local",
            "main.ts", "main.js", "server.js", "app.js",
            "app.module.ts", "nixpacks.toml"
        ]
        for root, dirs, files in os.walk(ws):
            if any(ig in root for ig in [".git", "node_modules", ".next", "dist", "build", ".cache"]):
                continue
            for f in files:
                if f in tracked_filenames:
                    fpath = os.path.join(root, f)
                    rel = os.path.relpath(fpath, ws).replace("\\", "/")
                    try:
                        with open(fpath, "r", encoding="utf-8") as fp:
                            context["key_files"][rel] = fp.read(8000)
                    except Exception:
                        pass

        all_logs = f"{context['build_logs']}\n{context['pod_runtime_logs']}"
        extracted_files = extract_culprit_files_from_logs(all_logs, ws)
        for rel_k, content_v in extracted_files.items():
            context["key_files"][rel_k] = content_v

    return context


def query_hermes_diagnostics(context: Dict[str, Any], preferred_model: Optional[str] = None, user_note: Optional[str] = None) -> Dict[str, Any]:
    system_instruction = """
Bạn là Hermes Agent - Kỹ sư trưởng AI SRE & Kubernetes Specialist của nền tảng Vibe Hosting (K3s, Docker, Nixpacks, Node.js, Spring Boot, Python, PostgreSQL/MySQL).

══════════════════════════════════════════════════════════════════════════════
NGUYÊN TẮC BẤT DI BẤT DỊCH: INFRASTRUCTURE-FIRST (ƯU TIÊN HẠ TẦNG K3S)
══════════════════════════════════════════════════════════════════════════════
Mục tiêu tối thượng: Khắc phục sự cố NHANH NHẤT (5-10 giây) và AN TOÀN NHẤT mà KHÔNG ĐƯỢC CHẠM VÀO MÃ NGUỒN nếu có thể giải quyết ở tầng hạ tầng.

1. ƯU TIÊN TUYỆT ĐỐI SỐ 1: BƠM BIẾN MÔI TRƯỜNG K8S ('set_env')
   - Bơm các biến `DB_HOST`, `DB_PORT`, `PORT=8080`, `JWT_SECRET`... vào Deployment K8s.

2. CẤU HÌNH TÀI NGUYÊN KUBERNETES / INGRESS / TRAEFIK MIDDLEWARE ('apply_manifest'):
   - Dùng khi cần tạo Middleware Traefik (như addPrefix `/api`, stripPrefix, CORS, rate-limit) hoặc cập nhật Ingress.
   - ⚠️ LƯU Ý K3S TRAEFIK: API Version chuẩn của Middleware K3s là 'traefik.io/v1alpha1' (KHÔNG DÙNG 'traefik.containo.us/v1alpha1').
   - ⚠️ CHÍNH XÁC TÊN TÀI NGUYÊN: Phải nhìn vào mục [DANH SÁCH INGRESS & SERVICE ĐANG CÓ] để lấy đúng tên Ingress (ví dụ: shop3t-be-ingress, car-booking-ingress), TUYỆT ĐỐI KHÔNG ĐƯỢC ĐOÁN MÒ TÊN INGRESS.

3. QUY TẮC CẤM ĐỐI VỚI 'exec_pod':
   - ⚠️ 'exec_pod' CHỈ ĐƯỢC DÙNG để chạy các lệnh nội bộ của dự án (ví dụ: `npm run seed`, `npx prisma db push`, `python manage.py migrate`).
   - ⛔ TUYỆT ĐỐI KHÔNG BAO GIỜ chạy lệnh `kubectl` bên trong 'exec_pod'. Pod ứng dụng chỉ là container Node.js/Python, KHÔNG CÓ CÔNG CỤ 'kubectl'. Mọi thao tác K8s phải dùng 'set_env' hoặc 'apply_manifest'.

4. 'edit_file' LÀ BIỆN PHÁP CUỐI CÙNG (LAST RESORT):
   - CHỈ ĐƯỢC PHÉP dùng 'edit_file' khi lỗi cú pháp không chạy được. Luôn đi kèm 'rebuild'.

BẮT BUỘC TRẢ VỀ JSON THUẦN (Valid JSON Only), KHÔNG DÙNG MARKDOWN FENCES:
{
  "diagnosis": "Phân tích nguyên nhân súc tích bằng tiếng Việt",
  "root_cause": "Mã lỗi chuẩn (vd: DB_CREDENTIALS_MISMATCH, PORT_CONFLICT, PREFIX_PATH_MISMATCH)",
  "suggested_solution": "Tóm tắt hướng giải quyết",
  "actions": [
    {
      "action_type": "set_env | apply_manifest | restart_deployment | exec_pod | edit_file | rebuild",
      "target": "Tên deployment hoặc tài nguyên K8s",
      "path": "Đường dẫn file nếu là edit_file",
      "content": "Nội dung YAML nếu là apply_manifest hoặc nội dung code nếu là edit_file",
      "command": "Lệnh bash nội bộ của ứng dụng nếu là exec_pod (KHÔNG DÙNG KUBECTL)",
      "env_vars": {"KEY": "VALUE"},
      "description": "Mô tả hành động bằng tiếng Việt"
    }
  ]
}
"""

    prompt = f"""
[THÔNG TIN DỰ ÁN]:
App: {context.get('app_name')}
Component: {context.get('component')}
User Note: {user_note or 'Không có ghi chú thêm'}

[DANH SÁCH INGRESS & SERVICE ĐANG CÓ TRÊN K8S]:
Ingresses:
{context.get('k8s_ingresses') or 'Không có ingress nào'}

Services:
{context.get('k8s_services') or 'Không có service nào'}

[LOG BUILD GẦN NHẤT]:
{context.get('build_logs') or 'Không có build log.'}

[LOG CONTAINER RUNTIME]:
{context.get('pod_runtime_logs') or 'Không có pod runtime log.'}

[SỰ KIỆN KUBERNETES]:
{context.get('k8s_events') or 'Không có sự kiện bất thường.'}

[CÁC FILE CẤU HÌNH & FILE NGUỒN LIÊN QUAN TRÍCH XUẤT ĐƯỢC]:
{json.dumps(context.get('key_files', {}), ensure_ascii=False, indent=2)}
"""

    models_to_try = [preferred_model] if preferred_model else []
    for m in FALLBACK_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    payload = {
        "messages": [
            {"role": "system", "content": system_instruction.strip()},
            {"role": "user", "content": prompt.strip()}
        ]
    }

    last_error = ""
    for model_name in models_to_try:
        payload["model"] = model_name
        try:
            write_log(context.get("app_name", "system"), f"🤖 [Hermes AI] Đang gửi phân tích tới Model [{model_name}]...")
            resp = requests.post(HERMES_API_URL, headers=HEADERS, json=payload, timeout=75)

            if resp.status_code == 200:
                raw_text = resp.json()["choices"][0]["message"]["content"].strip()
                json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                parsed_data = None
                if json_match:
                    try:
                        parsed_data = json.loads(json_match.group(0))
                    except json.JSONDecodeError:
                        pass
                if not parsed_data:
                    clean_text = raw_text.strip()
                    clean_text = re.sub(r"^`{1,3}(?:json)?\s*", "", clean_text, flags=re.IGNORECASE)
                    clean_text = re.sub(r"\s*`{1,3}$", "", clean_text)
                    parsed_data = json.loads(clean_text.strip())

                if "actions" in parsed_data and isinstance(parsed_data["actions"], list):
                    has_edit_file = False
                    has_rebuild = False

                    for act in parsed_data["actions"]:
                        if "file_path" in act and not act.get("path"):
                            act["path"] = act["file_path"]
                        if "path" in act and not act.get("file_path"):
                            act["file_path"] = act["path"]
                        if "diff" in act and not act.get("content"):
                            act["content"] = act["diff"]
                        if "content" in act and not act.get("diff"):
                            act["diff"] = act["content"]

                        if act.get("action_type") == "edit_file":
                            has_edit_file = True
                        if act.get("action_type") == "rebuild":
                            has_rebuild = True

                    if has_edit_file and not has_rebuild:
                        parsed_data["actions"] = [
                            a for a in parsed_data["actions"]
                            if a.get("action_type") != "restart_deployment"
                        ]
                        parsed_data["actions"].append({
                            "action_type": "rebuild",
                            "target": context.get("app_name"),
                            "description": "Đóng gói lại Docker Image mới và cập nhật Pod (Rebuild)",
                        })

                    parsed_data["actions"].sort(key=lambda a: ACTION_PRIORITY.get(a.get("action_type", ""), 99))

                return parsed_data

            elif resp.status_code == 429:
                write_log(context.get("app_name", "system"), f"⚠️ Model [{model_name}] gặp Rate Limit 429. Đang chuyển sang model tiếp theo...")
                continue
            else:
                last_error = f"Model {model_name} HTTP {resp.status_code}: {resp.text}"
                continue
        except Exception as e:
            last_error = str(e)
            continue

    raise HTTPException(status_code=500, detail=f"Hermes Agent API không phản hồi hoặc quá tải: {last_error}")


def execute_remediation_action(app_name: str, action: RemediationAction) -> Dict[str, Any]:
    comp = detect_action_component(action, default_comp="be")
    ws = resolve_workspace(app_name, comp)

    # 1. Áp dụng Kubernetes Manifest trực tiếp trên Host VPS
    if action.action_type == "apply_manifest":
        manifest_yaml = action.content or action.manifest
        if not manifest_yaml or not manifest_yaml.strip():
            return {"status": "error", "message": "Nội dung Manifest YAML rỗng."}

        # Tự động sửa API Version cũ của Traefik nếu Hermes vô tình dùng traefik.containo.us
        if "traefik.containo.us/v1alpha1" in manifest_yaml:
            manifest_yaml = manifest_yaml.replace("traefik.containo.us/v1alpha1", "traefik.io/v1alpha1")

        try:
            res = subprocess.run(
                ["kubectl", "apply", "-n", K8S_NAMESPACE, "-f", "-"],
                input=manifest_yaml,
                text=True,
                capture_output=True,
                timeout=30
            )
            if res.returncode == 0:
                output_msg = res.stdout.strip()
                return {
                    "status": "success",
                    "output": output_msg,
                    "message": f"Đã áp dụng thành công K8s Manifest trên Host VPS: {output_msg}"
                }
            return {
                "status": "error",
                "output": res.stderr.strip(),
                "message": f"Lỗi khi apply K8s Manifest: {res.stderr.strip()}"
            }
        except Exception as e:
            return {"status": "error", "message": f"Lỗi thực thi kubectl apply: {str(e)}"}

    # 2. Sửa file mã nguồn
    elif action.action_type == "edit_file":
        if not ws:
            return {"status": "error", "message": f"Không tìm thấy thư mục workspace cho component [{comp}]."}
        
        target_path = action.path or action.file_path
        target_content = action.content if action.content is not None else action.diff

        if not target_path or target_content is None:
            return {"status": "error", "message": "Thiếu đường dẫn (path/file_path) hoặc nội dung file (content/diff)."}

        # Làm sạch tiền tố frontend/ hoặc backend/ nếu path bị lồng
        clean_rel_path = target_path.strip("/\\")
        for prefix in ["frontend/", "fe/", "backend/", "be/"]:
            if clean_rel_path.startswith(prefix):
                clean_rel_path = clean_rel_path[len(prefix):].strip("/\\")
                break

        full_path = os.path.abspath(os.path.join(ws, clean_rel_path))
        if not full_path.startswith(ws):
            return {"status": "error", "message": "Từ chối truy cập: Đường dẫn nằm ngoài workspace."}

        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(target_content)

            # Tự động commit vào Git local để đánh dấu sửa đổi
            try:
                subprocess.run(["git", "add", clean_rel_path], cwd=ws, check=False)
                subprocess.run(
                    ["git", "commit", "-m", f"Hermes Auto-heal: Fix {clean_rel_path}"],
                    cwd=ws, check=False
                )
            except Exception:
                pass

            return {
                "status": "success",
                "path": clean_rel_path,
                "message": f"Đã cập nhật mã nguồn file {clean_rel_path} thành công."
            }
        except Exception as e:
            return {"status": "error", "message": f"Lỗi ghi file: {str(e)}"}

    # 3. Chạy lệnh Pod an toàn (TỰ ĐỘNG CHUYỂN KUBECTL RA NGOÀI HOST VPS NẾU HERMES GỬI NHẦM)
    elif action.action_type == "exec_pod":
        safe_command = (action.command or "").strip()
        if not safe_command:
            return {"status": "error", "message": "Lệnh thực thi rỗng."}

        # ⚡ CƠ CHẾ SMART-ROUTE: Nếu Hermes lỡ đưa lệnh kubectl vào exec_pod,
        # hệ thống tự động đánh chặn và chuyển ra ngoài Host VPS để chạy thành công 100%!
        if "kubectl " in safe_command or safe_command.startswith("kubectl"):
            write_log(app_name, f"⚡ [Hermes Smart-Route] Phát hiện lệnh K8s trong exec_pod, tự động chuyển ra Host VPS: {safe_command[:60]}...")
            
            # Tự động chuẩn hóa traefik API version nếu có trong lệnh
            if "traefik.containo.us" in safe_command:
                safe_command = safe_command.replace("traefik.containo.us/v1alpha1", "traefik.io/v1alpha1")

            ing_match = re.search(r'\bingress(?:es)?(?:\.networking\.k8s\.io)?\s+([a-zA-Z0-9_-]+)', safe_command)
            if ing_match:
                cand_ing = ing_match.group(1)
                real_ing = find_actual_k8s_resource("ingress", cand_ing, app_name)
                if real_ing and real_ing != cand_ing:
                    write_log(app_name, f"⚡ [Hermes Auto-Correct] Tự động sửa tên Ingress: '{cand_ing}' -> '{real_ing}'")
                    safe_command = safe_command.replace(cand_ing, real_ing)

            dep_match = re.search(r'\bdeployment(?:s)?(?:\.apps)?\s+([a-zA-Z0-9_-]+)', safe_command)
            if dep_match:
                cand_dep = dep_match.group(1)
                real_dep = find_actual_k8s_resource("deployment", cand_dep, app_name)
                if real_dep and real_dep != cand_dep:
                    write_log(app_name, f"⚡ [Hermes Auto-Correct] Tự động sửa tên Deployment: '{cand_dep}' -> '{real_dep}'")
                    safe_command = safe_command.replace(cand_dep, real_dep)

            try:
                host_res = subprocess.run(
                    safe_command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    executable="/bin/bash"
                )
                combined_output = f"{host_res.stdout}\n{host_res.stderr}".strip()

                if host_res.returncode != 0 and "NotFound" in combined_output:
                    not_found_m = re.search(r'ingresses(?:\.networking\.k8s\.io)?\s+"([^"]+)"\s+not found', combined_output)
                    if not_found_m:
                        bad_name = not_found_m.group(1)
                        resolved_name = find_actual_k8s_resource("ingress", bad_name, app_name)
                        if resolved_name and resolved_name != bad_name:
                            retry_cmd = safe_command.replace(bad_name, resolved_name)
                            write_log(app_name, f"🔄 [Hermes Retry] Thử lại với Ingress thực tế '{resolved_name}'...")
                            retry_res = subprocess.run(
                                retry_cmd, shell=True, text=True, capture_output=True, timeout=60, executable="/bin/bash"
                            )
                            combined_output = f"{retry_res.stdout}\n{retry_res.stderr}".strip()
                            if retry_res.returncode == 0:
                                return {
                                    "status": "success",
                                    "output": combined_output,
                                    "message": f"Đã tự động nhận diện và gắn Ingress [{resolved_name}] thành công: {combined_output}"
                                }

                if host_res.returncode == 0:
                    return {
                        "status": "success",
                        "output": combined_output,
                        "message": f"Đã thực thi thành công lệnh K8s trên Host VPS: {combined_output or 'Thành công'}"
                    }
                return {
                    "status": "error",
                    "output": combined_output,
                    "message": f"Lỗi thực thi lệnh K8s trên Host VPS (Exit code: {host_res.returncode}): {combined_output}"
                }
            except Exception as e:
                return {"status": "error", "message": f"Lỗi thực thi lệnh K8s trên Host: {str(e)}"}

        raw_target = action.target or (f"{app_name}-{comp}" if comp in ["fe", "be"] else app_name)
        target = sanitize_k8s_target(raw_target, "deployment", app_name)
        check_cmd = (
            f"kubectl get pods -n {K8S_NAMESPACE} "
            f"-l 'app in ({target}, {app_name}, {app_name}-{comp})' "
            f"-o jsonpath='{{.items[*].metadata.name}}'"
        )
        try:
            pod_names = subprocess.check_output(check_cmd, shell=True, text=True).strip().split()
            if not pod_names:
                return {
                    "status": "error",
                    "message": f"Không tìm thấy Pod hoạt động cho '{target}'."
                }

            pod_name = pod_names[0]
            wait_cmd = ["kubectl", "wait", "--for=condition=Ready", f"pod/{pod_name}", "-n", K8S_NAMESPACE, "--timeout=10s"]
            wait_res = subprocess.run(wait_cmd, capture_output=True, text=True)

            if wait_res.returncode != 0:
                return {
                    "status": "error",
                    "output": wait_res.stderr or wait_res.stdout,
                    "message": f"Pod '{pod_name}' đang bị crash hoặc chưa Ready, không thể mở kết nối Shell."
                }

            exec_res = subprocess.run(
                ["kubectl", "exec", pod_name, "-n", K8S_NAMESPACE, "--", "sh", "-c", safe_command],
                capture_output=True, text=True, timeout=180
            )

            raw_out = (exec_res.stdout or "").strip()
            raw_err = (exec_res.stderr or "").strip()
            combined_output = f"{raw_out}\n{raw_err}".strip()

            if exec_res.returncode != 0:
                return {
                    "status": "error",
                    "output": combined_output,
                    "message": f"Lệnh thực thi thất bại trên Pod {pod_name} (Exit code: {exec_res.returncode})."
                }

            return {
                "status": "success",
                "output": combined_output,
                "message": f"Đã thực thi thành công lệnh trên Pod {pod_name}."
            }

        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "Lệnh thực thi trên Pod bị quá thời gian chờ (Timeout 180s)."}
        except Exception as e:
            return {"status": "error", "message": f"Lỗi thực thi lệnh Pod: {str(e)}"}

    # 3. Đặt biến môi trường K8s
    elif action.action_type == "set_env":
        raw_target = action.target or (f"{app_name}-{comp}" if comp in ["fe", "be"] else app_name)
        target = sanitize_k8s_target(raw_target, "deployment", app_name)
        if not action.env_vars:
            return {"status": "error", "message": "Danh sách biến môi trường trống."}

        env_args = [f"{k}={v}" for k, v in action.env_vars.items()]
        cmd = ["kubectl", "set", "env", f"deployment/{target}", "-n", K8S_NAMESPACE] + env_args
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return {"status": "success", "message": f"Đã áp dụng biến môi trường vào deployment {target}."}
        return {"status": "error", "output": res.stderr, "message": f"Lỗi cập nhật env cho {target}: {res.stderr}"}

    # 4. Rebuild ứng dụng (Bọc an toàn try...except, không để văng lỗi 500)
    elif action.action_type == "rebuild":
        target_comp = detect_action_component(action, default_comp=comp)
        try:
            write_log(app_name, f"⚡ [Hermes Auto-heal] Bắt đầu Rebuild cho component [{target_comp.upper()}]...")
            redeploy_app(app_name, "main", pull_git=False, target_component=target_comp)
            return {
                "status": "success",
                "message": f"Đã Rebuild và cập nhật Pod thành công cho {app_name} [{target_comp.upper()}]."
            }
        except Exception as e:
            err_details = str(e)
            write_log(app_name, f"❌ [Hermes Auto-heal] Rebuild thất bại: {err_details}")
            return {
                "status": "error",
                "message": f"Rebuild thất bại: {err_details}"
            }

    # 5. Khởi động lại Deployment
    elif action.action_type == "restart_deployment":
        raw_target = action.target or (f"{app_name}-{comp}" if comp in ["fe", "be"] else app_name)
        target = sanitize_k8s_target(raw_target, "deployment", app_name)
        cmd = ["kubectl", "rollout", "restart", f"deployment/{target}", "-n", K8S_NAMESPACE]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return {"status": "success", "message": f"Đã kích hoạt khởi động lại deployment {target}."}
        return {"status": "error", "output": res.stderr, "message": f"Lỗi khởi động lại {target}: {res.stderr}"}

    return {"status": "error", "message": f"Hành động không xác định: {action.action_type}"}


def push_healed_workspace_to_git(app_name: str, component: str, token: str) -> Dict[str, Any]:
    """Tự động đẩy mã nguồn đã vá lỗi lên GitHub của người dùng qua Token"""
    ws = resolve_workspace(app_name, component)
    if not ws or not os.path.exists(ws):
        return {"status": "error", "message": "Không tìm thấy thư mục workspace để push Git."}

    try:
        # 1. Xác thực Token để lấy username
        gh_headers = {
            "Authorization": f"token {token.strip()}",
            "Accept": "application/vnd.github.v3+json",
        }
        user_res = requests.get("https://api.github.com/user", headers=gh_headers, timeout=10)
        if user_res.status_code != 200:
            return {"status": "error", "message": "GitHub Token không hợp lệ để tự động push."}

        username = user_res.json().get("login")

        # 2. Lấy tên repo hiện tại từ remote origin
        raw_remote = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=ws, text=True
        ).strip()
        clean_repo_name = raw_remote.split("/")[-1].replace(".git", "").strip()

        # 3. Lấy nhánh hiện tại (main/master)
        try:
            current_branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=ws, text=True
            ).strip()
        except Exception:
            current_branch = "main"

        # 4. Đặt lại remote URL với token và push
        authenticated_remote = f"https://{token.strip()}@github.com/{username}/{clean_repo_name}.git"
        subprocess.run(["git", "remote", "set-url", "origin", authenticated_remote], cwd=ws, check=True)
        subprocess.run(["git", "push", "-u", "origin", current_branch], cwd=ws, check=True, capture_output=True)

        public_repo_url = f"https://github.com/{username}/{clean_repo_name}"
        write_log(app_name, f"✅ [Hermes Auto-heal] Đã tự động push bản vá lên {public_repo_url} (nhánh {current_branch})!")
        return {
            "status": "success",
            "repo_url": public_repo_url,
            "branch": current_branch,
            "user": username,
            "message": f"Đã lưu vĩnh viễn bản vá vào GitHub của [{username}]"
        }
    except Exception as e:
        write_log(app_name, f"⚠️ [Hermes Auto-heal] Push Git tự động không thành công: {str(e)}")
        return {"status": "error", "message": str(e)}


@router.post("/analyze")
async def analyze_app_issues(payload: AIDebugRequest):
    context = collect_debug_context(payload.app_name, payload.component or "be")
    ai_result = query_hermes_diagnostics(context, payload.preferred_model, payload.user_note)

    execution_results = []
    git_push_result = None
    has_edit_file = False

    if payload.auto_apply and "actions" in ai_result:
        for act_raw in ai_result["actions"]:
            action = RemediationAction(**act_raw)
            if action.action_type == "edit_file":
                has_edit_file = True
            res = execute_remediation_action(payload.app_name, action)
            execution_results.append({
                "action": action.description,
                "result": res
            })

        # Tự động push nếu có token và có sửa file
        if payload.auto_push and payload.github_token and has_edit_file:
            git_push_result = push_healed_workspace_to_git(
                payload.app_name,
                payload.component or "be",
                payload.github_token
            )

    return {
        "status": "success",
        "app_name": payload.app_name,
        "diagnosis": ai_result.get("diagnosis"),
        "root_cause": ai_result.get("root_cause"),
        "suggested_solution": ai_result.get("suggested_solution"),
        "actions": ai_result.get("actions", []),
        "applied": payload.auto_apply,
        "execution_results": execution_results,
        "git_push_result": git_push_result
    }


@router.post("/{app_name}/execute")
async def execute_approved_actions(app_name: str, payload: ExecuteActionsPayload):
    try:
        sorted_actions = sorted(payload.actions, key=lambda a: ACTION_PRIORITY.get(a.action_type, 99))

        results = []
        has_edit_file = False
        target_comp = "be"

        for action in sorted_actions:
            if action.action_type == "edit_file":
                has_edit_file = True
                target_comp = detect_action_component(action, default_comp="be")
            
            try:
                res = execute_remediation_action(app_name, action)
                results.append({
                    "action": action.description,
                    "type": action.action_type,
                    "result": res
                })
            except Exception as act_err:
                write_log(app_name, f"⚠️ Lỗi thực thi action [{action.action_type}]: {str(act_err)}")
                results.append({
                    "action": action.description,
                    "type": action.action_type,
                    "result": {
                        "status": "error",
                        "message": f"Lỗi nội bộ khi thực thi hành động: {str(act_err)}"
                    }
                })

        git_push_result = None
        if payload.auto_push and payload.github_token and has_edit_file:
            git_push_result = push_healed_workspace_to_git(
                app_name,
                target_comp,
                payload.github_token
            )

        return {
            "status": "success",
            "app_name": app_name,
            "results": results,
            "git_push_result": git_push_result
        }
    except Exception as e:
        return {
            "status": "error",
            "app_name": app_name,
            "message": f"Lỗi trong quá trình thực thi: {str(e)}",
            "results": []
        }

@router.post("/chat")
async def chat_with_hermes(payload: AIChatRequest):
    """
    Endpoint dành riêng cho khung Chat liên tục. 
    Hỗ trợ History và trả về văn bản tự do thay vì ép kiểu JSON.
    """
    # 1. Thu thập bối cảnh mới nhất (Log, K8s, Code)
    context = collect_debug_context(payload.app_name, payload.component or "be")
    
    # 2. System Prompt dành riêng cho Chat
    system_instruction = """
Bạn là Hermes Agent - Trợ lý DevOps & AI SRE. Bạn đang trò chuyện trực tiếp với người dùng qua khung chat.
Bối cảnh hiện tại của ứng dụng đã được cung cấp bên dưới (bao gồm log, file cấu hình, sự kiện K8s).
Hãy trả lời các câu hỏi của người dùng một cách ngắn gọn, chuyên nghiệp. Hỗ trợ định dạng Markdown để làm nổi bật code hoặc câu lệnh.
KHÔNG cần trả về JSON, hãy trả lời như một người bình thường.
"""

    context_prompt = f"""
[BỐI CẢNH DỰ ÁN DÀNH CHO BẠN THAM KHẢO]:
App: {context.get('app_name')} | Component: {context.get('component')}
Log Build: {context.get('build_logs')[-1000:] if context.get('build_logs') else 'Trống'}
Log Runtime: {context.get('pod_runtime_logs')[-1500:] if context.get('pod_runtime_logs') else 'Trống'}
Ingresses: {context.get('k8s_ingresses')}
"""

    # 3. Cấu trúc mảng tin nhắn (History + New Message)
    messages = [
        {"role": "system", "content": system_instruction.strip() + "\n\n" + context_prompt.strip()}
    ]
    
    # Đẩy lịch sử chat cũ vào (giới hạn 10 tin nhắn gần nhất để tránh quá tải token)
    for msg in payload.history[-10:]:
        # API của một số LLM dùng "model" thay vì "assistant"
        role = "assistant" if msg.role == "assistant" else "user"
        messages.append({"role": role, "content": msg.content})
        
    # Đẩy tin nhắn mới nhất
    messages.append({"role": "user", "content": payload.message})

    # 4. Gọi LLM
    models_to_try = [payload.preferred_model] if payload.preferred_model else FALLBACK_MODELS
    last_error = ""
    
    req_payload = {"messages": messages}

    for model_name in models_to_try:
        req_payload["model"] = model_name
        try:
            write_log(payload.app_name, f"💬 [Hermes Chat] Gửi tin nhắn tới Model [{model_name}]...")
            resp = requests.post(HERMES_API_URL, headers=HEADERS, json=req_payload, timeout=60)

            if resp.status_code == 200:
                reply_text = resp.json()["choices"][0]["message"]["content"].strip()
                return {
                    "status": "success",
                    "reply": reply_text
                }
            elif resp.status_code == 429:
                continue
            else:
                last_error = f"Model HTTP {resp.status_code}: {resp.text}"
                continue
        except Exception as e:
            last_error = str(e)
            continue

    raise HTTPException(status_code=500, detail=f"Hermes Chat API quá tải: {last_error}")
