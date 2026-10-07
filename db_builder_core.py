import os
import subprocess
import time
import random
import string
import json
import re
import requests

try:
    import yaml
except ImportError:
    yaml = None

from builder_core import write_log, run_cmd, DOCKER_USER, BASE_WORKSPACE, K8S_NAMESPACE, HERMES_API_URL, HEADERS, FALLBACK_MODELS

def generate_db_password(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

# --- HÀM TỰ ĐỘNG VÁ XUNG ĐỘT ESM & COMMONJS CHO REPO NODEJS BÊN THỨ BA ---
def auto_patch_nodejs_esm_conflicts(workspace_dir: str, app_name: str):
    """
    Tự động chuẩn hóa cú pháp 'export const/function' sang CommonJS (module.exports)
    nếu repository không khai báo 'type': 'module' trong package.json.
    """
    pkg_path = os.path.join(workspace_dir, "package.json")
    if not os.path.exists(pkg_path):
        return

    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg_data = json.load(f)
            
        # Nếu project chạy chế độ CommonJS thuần
        if pkg_data.get("type") != "module":
            scan_dirs = [os.path.join(workspace_dir, "src"), workspace_dir]
            patched_files = set()

            for target_dir in scan_dirs:
                if not os.path.exists(target_dir):
                    continue
                for root, _, files in os.walk(target_dir):
                    if any(ign in root for ign in [".git", "node_modules", "dist", "build", "k8s"]):
                        continue
                    for file in files:
                        if file.endswith(".js"):
                            fpath = os.path.join(root, file)
                            if fpath in patched_files:
                                continue
                            try:
                                with open(fpath, "r", encoding="utf-8") as jf:
                                    content = jf.read()
                                
                                # Phát hiện từ khóa export const / let / var / function trong CommonJS
                                if re.search(r'^\s*export\s+(?:const|let|var|function|default)\s+', content, re.MULTILINE):
                                    write_log(app_name, f"   -> [Auto-Fix] Chuẩn hóa ESM sang CommonJS tại: {file}")
                                    
                                    # Lấy danh sách biến/hàm được export
                                    exported_vars = re.findall(r'^\s*export\s+(?:const|let|var|function)\s+([a-zA-Z0-9_$]+)', content, flags=re.MULTILINE)
                                    
                                    # Chuyển export const/function -> const/function
                                    content = re.sub(r'^\s*export\s+(const|let|var|function)\s+', r'\1 ', content, flags=re.MULTILINE)
                                    content = re.sub(r'^\s*export\s+default\s+', r'module.exports = ', content, flags=re.MULTILINE)
                                    
                                    if exported_vars and "module.exports" not in content:
                                        unique_vars = list(set(exported_vars))
                                        content += f"\n\nmodule.exports = {{\n  " + ",\n  ".join(unique_vars) + "\n};\n"
                                    
                                    with open(fpath, "w", encoding="utf-8") as jf:
                                        jf.write(content)
                                    patched_files.add(fpath)
                            except Exception:
                                pass
    except Exception as e:
        write_log(app_name, f"⚠️ Cảnh báo Auto-Patch Node.js: {e}")

# --- HÀM TỰ ĐỘNG NHẬN DIỆN CỔNG CHÍNH XÁC THEO MÃ NGUỒN ---
def detect_app_port(workspace_dir: str, ai_suggested_port: int = None) -> int:
    # 1. Java Spring Boot -> Mặc định 8080
    if os.path.exists(os.path.join(workspace_dir, "pom.xml")) or os.path.exists(os.path.join(workspace_dir, "build.gradle")):
        for root, _, files in os.walk(workspace_dir):
            for f in files:
                if f in ["application.properties", "application.yml", "application.yaml"]:
                    try:
                        with open(os.path.join(root, f), "r", encoding="utf-8") as pf:
                            m = re.search(r'(?:server\.port)\s*[:=]\s*(\d+)', pf.read())
                            if m:
                                return int(m.group(1))
                    except Exception:
                        pass
        return 8080

    # 2. NodeJS / Frontend SPA (React, Vite, Vue) / Express / NestJS
    pkg_path = os.path.join(workspace_dir, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg_data = json.load(f)
            deps = {**pkg_data.get("dependencies", {}), **pkg_data.get("devDependencies", {})}
            
            for env_name in [".env", ".env.local", ".env.production"]:
                env_file = os.path.join(workspace_dir, env_name)
                if os.path.exists(env_file):
                    with open(env_file, "r", encoding="utf-8") as ef:
                        m = re.search(r'^\s*PORT\s*=\s*(\d+)', ef.read(), re.MULTILINE)
                        if m:
                            return int(m.group(1))

            is_spa = any(k in deps for k in ["vite", "react", "vue", "@angular/core", "svelte"])
            has_node_server = any(k in deps for k in ["express", "@nestjs/core", "koa", "fastify", "socket.io"])
            
            if is_spa and not has_node_server:
                return 3000
            elif has_node_server:
                return 3000 if not ai_suggested_port else ai_suggested_port
        except Exception:
            pass

    # 3. Python
    if os.path.exists(os.path.join(workspace_dir, "requirements.txt")) or os.path.exists(os.path.join(workspace_dir, "Pipfile")):
        return 8000 if not ai_suggested_port else ai_suggested_port

    # 4. PHP
    if os.path.exists(os.path.join(workspace_dir, "composer.json")) or os.path.exists(os.path.join(workspace_dir, "index.php")):
        return 80

    if ai_suggested_port and isinstance(ai_suggested_port, int) and ai_suggested_port > 0:
        return ai_suggested_port
        
    return 8080

# --- HÀM TRỘN CẤU HÌNH THÔNG MINH (BẢO TỒN DỮ LIỆU CỦA USER) ---
def merge_properties_content(orig_content: str, new_content: str) -> str:
    new_props = {}
    for line in new_content.splitlines():
        line_str = line.strip()
        if line_str and not line_str.startswith(('#', '!')):
            if '=' in line_str:
                k, v = line_str.split('=', 1)
                new_props[k.strip()] = v.strip()
            elif ':' in line_str:
                k, v = line_str.split(':', 1)
                new_props[k.strip()] = v.strip()
                
    if not new_props:
        return orig_content
        
    updated_lines = []
    seen_keys = set()
    for line in orig_content.splitlines():
        line_str = line.strip()
        if line_str and not line_str.startswith(('#', '!')):
            delimiter = '=' if '=' in line_str else (':' if ':' in line_str else None)
            if delimiter:
                k, _ = line_str.split(delimiter, 1)
                k = k.strip()
                if k in new_props:
                    updated_lines.append(f"{k}={new_props[k]}")
                    seen_keys.add(k)
                    continue
        updated_lines.append(line)
        
    for k, v in new_props.items():
        if k not in seen_keys:
            updated_lines.append(f"{k}={v}")
            
    return "\n".join(updated_lines) + "\n"

# --- HÀM GỌI AI ---
def call_hermes_ai(prompt_text: str, app_name: str) -> dict:
    payload = {
        "messages": [
            {"role": "user", "content": prompt_text}
        ]
    }
    for model_name in FALLBACK_MODELS:
        payload["model"] = model_name
        try:
            write_log(app_name, f"⏳ Đang kết nối Model: [{model_name}]...")
            response = requests.post(HERMES_API_URL, headers=HEADERS, json=payload, timeout=60)
            if response.status_code == 200:
                content = response.json().get('choices', [{}])[0].get('message', {}).get('content')
                if content:
                    json_match = re.search(r'\{.*\}', content, re.DOTALL)
                    if not json_match:
                        raise Exception("Không tìm thấy cấu trúc JSON hợp lệ trong phản hồi.")
                    raw_json = json_match.group(0)
                    raw_json = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', raw_json)
                    ai_data = json.loads(raw_json)
                    write_log(app_name, f"✅ Model [{model_name}] xử lý thành công!")
                    return ai_data
            write_log(app_name, f"⚠️ Model [{model_name}] phản hồi không khả dụng. Thử model khác...")
        except Exception as e:
            write_log(app_name, f"⚠️ Model [{model_name}] gặp lỗi: {str(e)}. Thử lại...")
            
    raise Exception("TẤT CẢ các AI Model dự phòng đều bị lỗi.")

# --- BỘ ĐỊNH TUYẾN DATABASE ---
def get_db_strategy(db_type: str, app_name: str, db_password: str, workspace_dir: str):
    db_type = db_type.lower()
    app_user = app_name.replace("-", "_")[:16]
    
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
            "connection_info": {"host": f"{app_name}-db-svc", "user": app_user, "port": 3306, "type": "mysql"}
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
            "connection_info": {"host": f"{app_name}-db-svc", "user": app_user, "port": 3306, "type": "mariadb"}
        }
    elif db_type == "postgresql":
        return {
            "image": "postgres:15",
            "port": 5432,
            "db_name_env": "POSTGRES_DB",
            "db_pass_env": "POSTGRES_PASSWORD",
            "db_user": app_user,
            "mount_path": "/var/lib/postgresql/data",
            "extra_env": [{"name": "POSTGRES_USER", "value": app_user}],
            "connection_info": {"host": f"{app_name}-db-svc", "user": app_user, "port": 5432, "type": "postgresql"}
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
            "connection_info": {"host": f"{app_name}-db-svc", "user": "root", "port": 27017, "type": "mongodb", "uri": f"mongodb://root:{{password}}@{app_name}-db-svc:27017/{app_name}_db?authSource=admin"}
        }
    elif db_type == "redis":
        return {
            "image": "redis:7.2",
            "port": 6379,
            "db_name_env": "REDIS_DB_DUMMY", 
            "db_pass_env": "REDIS_PASSWORD_DUMMY",
            "db_user": "default",
            "mount_path": "/data",
            "custom_command": ["redis-server", "--requirepass", db_password, "--appendonly", "yes"],
            "connection_info": {"host": f"{app_name}-db-svc", "user": "default", "port": 6379, "type": "redis"}
        }
    else:
        raise ValueError(f"Loại Database '{db_type}' chưa được hỗ trợ.")

def generate_db_manifest(app_name: str, db_password: str, workspace_dir: str, strategy: dict):
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
    command_yaml = ""
    if "custom_command" in strategy:
        cmd_list = [f'"{c}"' for c in strategy['custom_command']]
        command_yaml = f"        command: [{', '.join(cmd_list)}]"

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
{command_yaml}
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

# --- TEMPLATE CHUẨN KUBERNETES MANIFESTS ---
def generate_standard_deployment(app_name: str, image_tag: str, target_port: int, db_info: dict = None) -> str:
    env_lines = [
        f'        - name: PORT\n          value: "{target_port}"',
        f'        - name: SERVER_PORT\n          value: "{target_port}"',
    ]

    if db_info:
        db_type = db_info.get("type", "").lower()
        db_host = db_info.get("host", "")
        db_port = str(db_info.get("port", 3306))
        db_user = db_info.get("user", "")
        db_pass = db_info.get("password", "")
        db_name = db_info.get("db_name", "")

        env_lines.extend([
            f'        - name: DB_HOST\n          value: "{db_host}"',
            f'        - name: DB_PORT\n          value: "{db_port}"',
            f'        - name: DB_USER\n          value: "{db_user}"',
            f'        - name: DB_PASS\n          value: "{db_pass}"',
            f'        - name: DB_PASSWORD\n          value: "{db_pass}"',
            f'        - name: DB_NAME\n          value: "{db_name}"',
        ])

        if db_type in ["mysql", "mariadb"]:
            jdbc_url = f"jdbc:mysql://{db_host}:{db_port}/{db_name}?createDatabaseIfNotExist=true&useSSL=false&allowPublicKeyRetrieval=true"
            env_lines.extend([
                f'        - name: DB_URL\n          value: "{jdbc_url}"',
                f'        - name: SPRING_DATASOURCE_URL\n          value: "{jdbc_url}"',
                f'        - name: SPRING_DATASOURCE_USERNAME\n          value: "{db_user}"',
                f'        - name: SPRING_DATASOURCE_PASSWORD\n          value: "{db_pass}"'
            ])
        elif db_type == "postgresql":
            jdbc_url = f"jdbc:postgresql://{db_host}:{db_port}/{db_name}"
            env_lines.extend([
                f'        - name: DB_URL\n          value: "{jdbc_url}"',
                f'        - name: SPRING_DATASOURCE_URL\n          value: "{jdbc_url}"',
                f'        - name: SPRING_DATASOURCE_USERNAME\n          value: "{db_user}"',
                f'        - name: SPRING_DATASOURCE_PASSWORD\n          value: "{db_pass}"'
            ])
        elif db_type == "mongodb":
            mongo_uri = db_info.get("uri", f"mongodb://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}?authSource=admin")
            env_lines.extend([
                f'        - name: MONGO_URI\n          value: "{mongo_uri}"',
                f'        - name: MONGODB_URI\n          value: "{mongo_uri}"',
                f'        - name: SPRING_DATA_MONGODB_URI\n          value: "{mongo_uri}"'
            ])

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

def generate_standard_service(app_name: str, target_port: int) -> str:
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

def generate_standard_ingress(app_name: str, app_domain: str) -> str:
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

# --- HÀM NẠP BACKUP DATABASE ---
def import_backup_to_db(db_type: str, db_pod_name: str, db_password: str, db_name: str, user: str, backup_file_path: str, app_name: str):
    if not backup_file_path or not os.path.exists(backup_file_path):
        return
    write_log(app_name, f"\n[DB Backup] Đang nạp dữ liệu từ file ({os.path.basename(backup_file_path)}) vào {db_type.upper()}...")
    if db_type in ["mysql", "mariadb"]:
        import_cmd = (
            f"sed -E '/^[[:space:]]*(CREATE DATABASE|USE)[[:space:]]+/Id' '{backup_file_path}' | "
            f"kubectl exec -i {db_pod_name} -n {K8S_NAMESPACE} -- "
            f"mysql -u {user} -p{db_password} {db_name}"
        )
    elif db_type == "postgresql":
        import_cmd = f"kubectl exec -i {db_pod_name} -n {K8S_NAMESPACE} -- psql -U {user} -d {db_name} < '{backup_file_path}'"
    elif db_type == "mongodb":
        import_cmd = f"kubectl cp '{backup_file_path}' {K8S_NAMESPACE}/{db_pod_name}:/tmp/dump_file && kubectl exec -i {db_pod_name} -n {K8S_NAMESPACE} -- mongorestore --username root --password {db_password} --authenticationDatabase admin --archive=/tmp/dump_file --gzip"
    else:
        write_log(app_name, "   -> Chưa hỗ trợ nạp tự động cho loại DB này.")
        return

    try:
        subprocess.run(import_cmd, shell=True, check=True, executable='/bin/bash')
        write_log(app_name, "   -> Nạp dữ liệu backup thành công 100%!")
    except Exception as e:
        write_log(app_name, f"   -> [CẢNH BÁO] Lỗi khi nạp file backup: {e}")

# --- AI VÁ MÃ NGUỒN CHO BACKEND ---
def analyze_and_fix_backend(workspace_dir: str, app_name: str, app_domain: str, db_info: dict):
    write_log(app_name, "[AI] Đang phân tích mã nguồn Backend và cấu hình kết nối DB...")
    code_context = ""
    for root, dirs, files in os.walk(workspace_dir):
        if any(ignored in root for ignored in [".git", "node_modules", "venv", "vendor", "target", "k8s"]):
            continue
        for file in files:
            if file in ["pom.xml", "build.gradle", "package.json", "requirements.txt", "composer.json", "application.properties", "application.yml", ".env", "database.php"]:
                try:
                    with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                        code_context += f"\n\n--- File: {file} ---\n" + f.read()[:2000]
                except Exception:
                    pass

    prompt = f"""
Bạn là Kỹ sư DevOps AI.
Nhiệm vụ: Phân tích Backend và cấu hình kết nối Database {db_info['type'].upper()}.
Tên dự án: {app_name}
Domain: {app_domain}

[THÔNG TIN DATABASE NỘI BỘ]
Loại DB: {db_info['type'].upper()}
Host: {db_info['host']}
User: {db_info['user']}
Password: {db_info['password']}
Database Name: {db_info['db_name']}
Port: {db_info['port']}

YÊU CẦU:
1. Sửa file cấu hình kết nối DB (application.properties, application.yml, .env,...).
2. TUYỆT ĐỐI GIỮ NGUYÊN BẢN toàn bộ cấu hình khác (MoMo, JWT, Mail,...). Chỉ sửa thông tin DB.
3. Cho biết cổng port mà Backend này lắng nghe (Mặc định 8080 với Spring Boot, 3000/5000/8000 tùy ngôn ngữ).

TRẢ VỀ DUY NHẤT ĐỐI TƯỢNG JSON (Không Markdown):
{{
  "files_to_update": [
    {{
      "path": "đường_dẫn_file_tương_đối",
      "content": "toàn_bộ_nội_dung_sau_khi_sửa"
    }}
  ],
  "app_port": 8080
}}
"""
    ai_port = None
    try:
        ai_data = call_hermes_ai(prompt, app_name)
        if "files_to_update" in ai_data:
            for file_data in ai_data["files_to_update"]:
                raw_path = file_data['path'].lstrip('/')
                file_path = os.path.join(workspace_dir, raw_path)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                final_content = file_data["content"]
                if raw_path.endswith(('.properties', '.env')) and os.path.exists(file_path):
                    try:
                        with open(file_path, "r", encoding="utf-8") as f_orig:
                            final_content = merge_properties_content(f_orig.read(), final_content)
                    except Exception:
                        pass
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(final_content)
        ai_port = ai_data.get("app_port")
    except Exception as e:
        write_log(app_name, f"⚠️ AI patch gặp cảnh báo: {e}. Sử dụng cấu hình mặc định.")

    detected_port = detect_app_port(workspace_dir, ai_port)
    write_log(app_name, f"   -> Cổng hoạt động của Backend: {detected_port}")
    return detected_port

# --- HÀM BƠM API BACKEND VÀO FRONTEND ---
def link_frontend_to_backend(workspace_dir: str, app_name: str, be_domain: str):
    write_log(app_name, f"[FE Linker] Đang liên kết Frontend tới Backend API (https://{be_domain})...")
    backend_url = f"https://{be_domain}"
    env_content = f"""
VITE_API_BASE_URL={backend_url}
VITE_API_URL={backend_url}
VITE_BACKEND_URL={backend_url}
REACT_APP_API_URL={backend_url}
NEXT_PUBLIC_API_URL={backend_url}
API_URL={backend_url}
"""
    for env_name in [".env", ".env.production", ".env.local"]:
        env_file = os.path.join(workspace_dir, env_name)
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                old_txt = f.read()
            merged = merge_properties_content(old_txt, env_content)
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(merged)
        else:
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(env_content.strip() + "\n")

    for root, _, files in os.walk(os.path.join(workspace_dir, "src") if os.path.exists(os.path.join(workspace_dir, "src")) else workspace_dir):
        if any(ign in root for ign in [".git", "node_modules", "dist", "build"]):
            continue
        for file in files:
            if file.endswith((".js", ".jsx", ".ts", ".tsx")):
                fpath = os.path.join(root, file)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        src_code = f.read()
                    if "http://localhost:8080" in src_code or "http://127.0.0.1:8080" in src_code:
                        write_log(app_name, f"   -> Thay thế hardcode localhost trong: {file}")
                        src_code = src_code.replace("http://localhost:8080", backend_url)
                        src_code = src_code.replace("http://127.0.0.1:8080", backend_url)
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(src_code)
                except Exception:
                    pass

# --- 1. LUỒNG DEPLOY APP ĐƠN LẺ CÓ DATABASE ---
def deploy_app_with_db(repo_url: str, app_name: str, app_domain: str, db_type: str, backup_file_path: str):
    write_log(app_name, f"\n=== [BẮT ĐẦU TRIỂN KHAI] {app_name} ({db_type.upper()}) ===")
    WORKSPACE_DIR = f"{BASE_WORKSPACE}/{app_name}"
    
    try:
        write_log(app_name, "[0] Đang kéo mã nguồn từ GitHub...")
        if os.path.exists(WORKSPACE_DIR):
             run_cmd(["rm", "-rf", WORKSPACE_DIR], app_name)
        run_cmd(["git", "clone", repo_url, WORKSPACE_DIR], app_name)

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
        
        write_log(app_name, f"[2] Đang triển khai Pod Database lên K3s...")
        run_cmd(["kubectl", "apply", "-f", f"k8s/db.yaml"], app_name, cwd=WORKSPACE_DIR)
        
        write_log(app_name, "[3] Đang chờ Pod Database khởi động...")
        db_pod_name = ""
        while True:
            try:
                db_pod_name = subprocess.check_output(f"kubectl get pods -n {K8S_NAMESPACE} -l app={app_name}-db -o jsonpath='{{.items[0].metadata.name}}'", shell=True, text=True).strip()
                status = subprocess.check_output(f"kubectl get pod {db_pod_name} -n {K8S_NAMESPACE} -o jsonpath='{{.status.phase}}'", shell=True, text=True).strip()
                if status == "Running":
                    time.sleep(15)
                    break
            except Exception:
                pass
            time.sleep(5)
            
        db_name = f"{app_name}_db".replace("-", "_")
        import_backup_to_db(db_type, db_pod_name, db_password, db_name, strategy["connection_info"]["user"], backup_file_path, app_name)
        
        db_info = strategy["connection_info"]
        db_info["password"] = db_password
        db_info["db_name"] = db_name

        write_log(app_name, "\n[5] Đang cấu hình ứng dụng...")
        detected_port = analyze_and_fix_backend(WORKSPACE_DIR, app_name, app_domain, db_info)
        
        # Tự động vá ESM/CommonJS cho Node.js
        auto_patch_nodejs_esm_conflicts(WORKSPACE_DIR, app_name)

        IMAGE_TAG = f"{DOCKER_USER}/{app_name}:latest"
        k8s_dir = f"{WORKSPACE_DIR}/k8s"
        with open(f"{k8s_dir}/deployment.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_deployment(app_name, IMAGE_TAG, detected_port, db_info))
        with open(f"{k8s_dir}/service.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_service(app_name, detected_port))
        with open(f"{k8s_dir}/ingress.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_ingress(app_name, app_domain))

        write_log(app_name, f"\n[6] Đang Build Docker Image ({IMAGE_TAG})...")
        run_cmd(["nixpacks", "build", ".", "--name", IMAGE_TAG], app_name, cwd=WORKSPACE_DIR)
        
        write_log(app_name, "[7] Đang đẩy Image lên Docker Hub...")
        run_cmd(["docker", "push", IMAGE_TAG], app_name)
        
        write_log(app_name, "[8] Đang khởi chạy Ứng dụng Web trên K3s...")
        run_cmd(["kubectl", "apply", "-f", "k8s/deployment.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/service.yaml"], app_name, cwd=WORKSPACE_DIR)
        run_cmd(["kubectl", "apply", "-f", "k8s/ingress.yaml"], app_name, cwd=WORKSPACE_DIR)
        
        write_log(app_name, f"\n=== HOÀN TẤT TRIỂN KHAI ===")
        write_log(app_name, f"Web App đang chạy tại: https://{app_domain}")
        write_log(app_name, "DONE")
        
    except Exception as e:
        write_log(app_name, f"\n[LỖI NGHIÊM TRỌNG] {e}")
        write_log(app_name, "ERROR")

# --- 2. LUỒNG DEPLOY FULLSTACK (2 REPO: FE + BE + DATABASE CHUNG) ---
def deploy_fullstack_app(app_name: str, be_repo_url: str, be_domain: str, fe_repo_url: str, fe_domain: str, db_type: str, backup_file_path: str):
    write_log(app_name, f"\n=== [BẮT ĐẦU TRIỂN KHAI FULLSTACK] {app_name} ===")
    BE_APP_NAME = f"{app_name}-be"
    FE_APP_NAME = f"{app_name}-fe"
    
    BE_WORKSPACE = f"{BASE_WORKSPACE}/{BE_APP_NAME}"
    FE_WORKSPACE = f"{BASE_WORKSPACE}/{FE_APP_NAME}"
    
    try:
        # BƯỚC 1: KHỞI TẠO DATABASE CHUNG
        write_log(app_name, f"\n--- BƯỚC 1: KHỞI TẠO DATABASE CHUNG ({db_type.upper()}) ---")
        db_password = generate_db_password()
        os.makedirs(BE_WORKSPACE, exist_ok=True)
        strategy = get_db_strategy(db_type, app_name, db_password, BE_WORKSPACE)
        generate_db_manifest(app_name, db_password, BE_WORKSPACE, strategy)
        
        run_cmd(["kubectl", "apply", "-f", f"{BE_WORKSPACE}/k8s/db.yaml"], app_name)
        
        db_pod_name = ""
        while True:
            try:
                db_pod_name = subprocess.check_output(f"kubectl get pods -n {K8S_NAMESPACE} -l app={app_name}-db -o jsonpath='{{.items[0].metadata.name}}'", shell=True, text=True).strip()
                status = subprocess.check_output(f"kubectl get pod {db_pod_name} -n {K8S_NAMESPACE} -o jsonpath='{{.status.phase}}'", shell=True, text=True).strip()
                if status == "Running":
                    time.sleep(15)
                    break
            except Exception:
                pass
            time.sleep(5)
            
        db_name = f"{app_name}_db".replace("-", "_")
        import_backup_to_db(db_type, db_pod_name, db_password, db_name, strategy["connection_info"]["user"], backup_file_path, app_name)
        
        db_info = strategy["connection_info"]
        db_info["password"] = db_password
        db_info["db_name"] = db_name

        # BƯỚC 2: BUILD VÀ DEPLOY BACKEND
        write_log(app_name, f"\n--- BƯỚC 2: TRIỂN KHAI BACKEND ({be_domain}) ---")
        if os.path.exists(BE_WORKSPACE):
            run_cmd(["rm", "-rf", BE_WORKSPACE], app_name)
        run_cmd(["git", "clone", be_repo_url, BE_WORKSPACE], app_name)
        
        be_port = analyze_and_fix_backend(BE_WORKSPACE, app_name, be_domain, db_info)
        
        # Tự động vá lỗi cú pháp Node.js cho Backend của repo thứ 3
        auto_patch_nodejs_esm_conflicts(BE_WORKSPACE, app_name)

        BE_IMAGE = f"{DOCKER_USER}/{BE_APP_NAME}:latest"
        be_k8s = f"{BE_WORKSPACE}/k8s"
        os.makedirs(be_k8s, exist_ok=True)
        with open(f"{be_k8s}/deployment.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_deployment(BE_APP_NAME, BE_IMAGE, be_port, db_info))
        with open(f"{be_k8s}/service.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_service(BE_APP_NAME, be_port))
        with open(f"{be_k8s}/ingress.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_ingress(BE_APP_NAME, be_domain))

        write_log(app_name, f"Đang build Backend Image ({BE_IMAGE})...")
        run_cmd(["nixpacks", "build", ".", "--name", BE_IMAGE], app_name, cwd=BE_WORKSPACE)
        run_cmd(["docker", "push", BE_IMAGE], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{be_k8s}/deployment.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{be_k8s}/service.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{be_k8s}/ingress.yaml"], app_name)
        write_log(app_name, f"✅ Backend đã online tại: https://{be_domain}")

        # BƯỚC 3: BUILD VÀ DEPLOY FRONTEND
        write_log(app_name, f"\n--- BƯỚC 3: TRIỂN KHAI FRONTEND ({fe_domain}) ---")
        if os.path.exists(FE_WORKSPACE):
            run_cmd(["rm", "-rf", FE_WORKSPACE], app_name)
        run_cmd(["git", "clone", fe_repo_url, FE_WORKSPACE], app_name)

        link_frontend_to_backend(FE_WORKSPACE, app_name, be_domain)
        fe_port = detect_app_port(FE_WORKSPACE, 3000)
        write_log(app_name, f"   -> Cổng hoạt động của Frontend: {fe_port}")
        
        FE_IMAGE = f"{DOCKER_USER}/{FE_APP_NAME}:latest"
        fe_k8s = f"{FE_WORKSPACE}/k8s"
        os.makedirs(fe_k8s, exist_ok=True)
        with open(f"{fe_k8s}/deployment.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_deployment(FE_APP_NAME, FE_IMAGE, fe_port, None))
        with open(f"{fe_k8s}/service.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_service(FE_APP_NAME, fe_port))
        with open(f"{fe_k8s}/ingress.yaml", "w", encoding="utf-8") as f:
            f.write(generate_standard_ingress(FE_APP_NAME, fe_domain))

        write_log(app_name, f"Đang build Frontend Image ({FE_IMAGE})...")
        run_cmd(["nixpacks", "build", ".", "--name", FE_IMAGE], app_name, cwd=FE_WORKSPACE)
        run_cmd(["docker", "push", FE_IMAGE], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{fe_k8s}/deployment.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{fe_k8s}/service.yaml"], app_name)
        run_cmd(["kubectl", "apply", "-f", f"{fe_k8s}/ingress.yaml"], app_name)
        write_log(app_name, f"✅ Frontend đã online tại: https://{fe_domain}")

        write_log(app_name, f"\n=== HOÀN TẤT TRIỂN KHAI CỤM FULLSTACK ===")
        write_log(app_name, f"Frontend: https://{fe_domain}")
        write_log(app_name, f"Backend : https://{be_domain}")
        write_log(app_name, "DONE")

    except Exception as e:
        write_log(app_name, f"\n[LỖI NGHIÊM TRỌNG FULLSTACK] {e}")
        write_log(app_name, "ERROR")

# --- HÀM DỌN DẸP SẠCH SẼ CẢ CỤM ---
def delete_app_db_resources(app_name: str):
    try:
        write_log(app_name, f"[Cleanup] Đang dọn dẹp cụm {app_name} (Database, Backend, Frontend)...")
        subprocess.run(["kubectl", "delete", "statefulset", f"{app_name}-db", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "service", f"{app_name}-db-svc", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "pvc", f"{app_name}-db-pvc", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(["kubectl", "delete", "pvc", f"db-storage-{app_name}-db-0", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        
        targets = [app_name, f"{app_name}-be", f"{app_name}-fe"]
        for target in targets:
            subprocess.run(["kubectl", "delete", "deployment,service,ingress", target, f"{target}-svc", f"{target}-ingress", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        
        subprocess.run(["kubectl", "delete", "all,pvc", "-l", f"app={app_name}-db", "-n", K8S_NAMESPACE, "--ignore-not-found=true"], check=False)
        subprocess.run(f"rm -f /opt/vibe-hosting/backups/{app_name}_*", shell=True, check=False)
        write_log(app_name, f"[Cleanup] Đã dọn dẹp hoàn tất toàn bộ tài nguyên của {app_name}!")
    except Exception as e:
        write_log(app_name, f"[CẢNH BÁO Cleanup] Lỗi: {e}")
