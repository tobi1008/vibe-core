# Vibe Core — Vibe Hosting API

Backend của nền tảng **Vibe Hosting**: nhận mã nguồn (link GitHub hoặc file nén), dùng AI để phân tích và sinh cấu hình, build Docker image bằng **Nixpacks**, đẩy lên **Docker Hub** rồi triển khai lên cụm **K3s** kèm tên miền và SSL tự động (Traefik + cert-manager / Let's Encrypt).

Ngoài deploy, API còn cung cấp: database nội bộ (MySQL, MariaDB, PostgreSQL, MongoDB, Redis), CI/CD qua GitHub Webhook, rollback, xem log build/runtime, File Manager sửa code trực tiếp và AI Debug (SRE agent) tự chẩn đoán và sửa lỗi.

- Framework: FastAPI (`Vibe Hosting API` v2.4.0)
- Cổng mặc định: `8080`
- CORS cho phép: `https://vibe.quyenlt.com` (frontend dashboard)

---

## Kiến trúc tổng quan

```
 Frontend (vibe.quyenlt.com)
          │  REST
          ▼
 ┌──────────────────────────┐        ┌──────────────────────────┐
 │  Vibe Core API (FastAPI) │──AI──▶ │ agy CLI / claude CLI /   │
 │  main.py + routers       │        │ Hermes API (OpenAI-compat)│
 └──────────┬───────────────┘        └──────────────────────────┘
            │ BackgroundTasks
            ▼
  git clone ─▶ auto-heal ─▶ AI sinh cấu hình ─▶ nixpacks build
            ─▶ docker push (Docker Hub) ─▶ kubectl apply (K3s)
                                                │
                              Deployment + Service + Ingress (Traefik, TLS)
                              StatefulSet DB + PVC + Service (nếu có DB)
```

Mọi tác vụ deploy chạy nền (`BackgroundTasks`). Tiến trình được ghi vào `/opt/vibe-hosting/logs/<app_name>.log`; dòng cuối là `DONE`, `ERROR` hoặc `DELETED` để frontend biết trạng thái.

---

## Cấu trúc mã nguồn

| File | Vai trò |
|---|---|
| [main.py](main.py) | Ứng dụng FastAPI, khai báo toàn bộ endpoint deploy / quản lý app / log / rollback / webhook. |
| [builder_core.py](builder_core.py) | Lõi dùng chung: cấu hình hệ thống, bộ điều phối AI (`call_ai_json`, `call_ai_prompt`), ghi log, `deploy_app` (deploy repo đơn), `delete_app`. |
| [db_builder_core.py](db_builder_core.py) | Deploy kèm database: sinh manifest StatefulSet/PVC, nhận diện cổng, import backup, sinh Deployment/Service/Ingress chuẩn, deploy fullstack 2 repo (FE + BE). |
| [cms_builder_core.py](cms_builder_core.py) | Deploy CMS / mã nguồn mở với database trắng (WordPress, Laravel, Ghost, repo tuỳ ý). Tự sinh `wp-config.php`, salt keys, `.env`. |
| [monorepo_builder_core.py](monorepo_builder_core.py) | Deploy monorepo: tự tìm thư mục backend/frontend, vá CORS, nối API URL cho frontend, sửa cấu hình Next.js. |
| [archive_builder_core.py](archive_builder_core.py) | Deploy từ file nén tải lên (`.zip`, `.tar.*`, `.rar`, `.7z`), giải nén an toàn (chống Zip Slip). |
| [cicd_core.py](cicd_core.py) | CI/CD: pull code, build image tag theo timestamp, `kubectl set image` (rolling update). Hỗ trợ single, fullstack và monorepo. |
| [file_manager_api.py](file_manager_api.py) | Router `/api/project`: xem cây thư mục, đọc/ghi file trong workspace, push code lên GitHub của người dùng. |
| [hermes_agent_router.py](hermes_agent_router.py) | Router `/api/ai-debug`: AI SRE thu thập log + sự kiện K8s, chẩn đoán, đề xuất và thực thi hành động sửa lỗi; chat với AI. |
| [security.py](security.py) | Bảo mật dùng chung: xác thực API Key, kiểm tra tên app / URL repo / tên file upload, xác minh chữ ký webhook, che token trong log. |
| [builder.py](builder.py) | Script CLI đời đầu (prototype) deploy một repo cố định. Không được API sử dụng. |
| [cluster-issuer.yaml](cluster-issuer.yaml) | `ClusterIssuer` Let's Encrypt (`letsencrypt-prod`) cho cert-manager, solver HTTP-01 qua Traefik. |
| [update.sh](update.sh) | Script cập nhật trên VPS: `git pull`, cài dependency, restart service `vibe-api`. |
| [.github/workflows/deploy.yml](.github/workflows/deploy.yml) | GitHub Actions: push lên `main` → SSH vào VPS và chạy `update.sh`. |

---

## AI Engines

Mỗi request deploy/debug nhận tham số `ai_provider`. Mỗi engine tự fallback lần lượt qua danh sách model khi model trước lỗi hoặc timeout.

| `ai_provider` | Engine | Cách gọi | Model |
|---|---|---|---|
| `agy` (mặc định) | Google Antigravity CLI | `agy -p <prompt> --model <m>` trên VPS | Gemini Flash 3.7 / 3.8 (5 cấp) |
| `claude` | Claude Code CLI | `claude -p <prompt> --model <m>` trên VPS | `opus`, `sonnet`, `haiku` |
| `hermes` | Hermes Agent | HTTP tới API OpenAI-compatible (`HERMES_API_URL`) | Các model miễn phí trên OpenRouter |

Danh sách model và mô tả trả về qua `GET /api/ai-providers`. Prompt quá 70.000 ký tự sẽ bị cắt để tránh lỗi `Argument list too long` của Linux.

---

## Các luồng deploy

### 1. Repo đơn — `POST /api/deploy`
1. Clone repo vào `/opt/vibe-hosting/workspaces/<app_name>`.
2. **Auto-heal IP**: thay `127.0.0.1` / `localhost` thành `0.0.0.0` trong file `.js .ts .py .json` để container nhận traffic.
3. Quét cấu trúc (`tree`) + các file cốt lõi (`package.json`, `requirements.txt`, `composer.json`, `vite.config.*`…), gửi cho AI.
4. AI trả JSON gồm `nixpacks.toml`, `deployment.yaml`, `service.yaml`, `ingress.yaml`, `auto_patches` (ví dụ thêm `allowedHosts` cho Vite) và `user_action_required`.
5. `nixpacks build` — nếu lỗi, log lỗi được gửi lại cho AI, **thử tối đa 3 lần**.
6. `docker push` → `kubectl apply`.

### 2. Repo + Database — `POST /api/deploy-with-db`
Tạo StatefulSet DB (`<app>-db`, PVC 1Gi, Service `<app>-db-svc`), chờ Pod chạy, import file backup nếu có, để AI cấu hình kết nối DB, rồi sinh Deployment chuẩn có sẵn các biến `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_URL`, `SPRING_DATASOURCE_*`, `MONGO_URI`… Khi deploy lại, mật khẩu DB cũ được giữ nguyên.

| `db_type` | Image | Cổng |
|---|---|---|
| `mysql` | `mysql:8.0` | 3306 |
| `mariadb` | `mariadb:10.11` | 3306 |
| `postgresql` | `postgres:15` | 5432 |
| `mongodb` | `mongo:6.0` | 27017 |
| `redis` | `redis:7.2` | 6379 |

Cổng ứng dụng được nhận diện tự động: PHP → 80, Java (Spring Boot) → 8080 hoặc `server.port`, Node.js → `PORT` trong `.env` hoặc 3000, Python → 8000.

### 3. Fullstack 2 repo — `POST /api/deploy-fullstack`
Một DB dùng chung, backend deploy thành `<app>-be`, frontend thành `<app>-fe`. Frontend được tự động gắn URL backend qua `.env` (`VITE_API_URL`, `REACT_APP_API_URL`, `NEXT_PUBLIC_API_URL`, …).

### 4. Monorepo — `POST /api/deploy-monorepo`
Tự tìm thư mục backend (`backend`, `server`, `api`…) và frontend (`frontend`, `client`, `web`…) hoặc đoán qua dependency trong `package.json`. Tạo DB trắng, vá CORS backend, sinh biến môi trường (JWT, CORS, `DATABASE_URL`) và build Node 22.

### 5. CMS / mã nguồn mở với DB trắng — `POST /api/deploy-open-source`
Template có sẵn (`GET /api/open-source-templates`): **WordPress**, **Laravel**, **Ghost**, **Custom repo**. Để trống `repo_url` hoặc truyền `wordpress` sẽ dùng repo WordPress chính thức. WordPress được tự sinh `wp-config.php`, 8 salt keys và cấu hình SSL sau reverse proxy Traefik.

### 6. File nén — `POST /api/deploy-archive`, `POST /api/deploy-archive-with-db`
Upload `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.rar`, `.7z`. Hệ thống giải nén an toàn, làm phẳng thư mục gốc lồng nhau rồi đi theo luồng deploy tương ứng. App từ file nén **không bật** Auto CI/CD.

---

## Tham chiếu API

**Xác thực:** mọi endpoint đều yêu cầu header `X-API-Key: <VIBE_API_KEY>`, trừ `/api/webhooks/*` (dùng chữ ký GitHub). Thiếu hoặc sai key trả về `401`; server chưa cấu hình `VIBE_API_KEY` trả về `503`.

**Ràng buộc đầu vào:**
- `app_name`: chữ thường, số, dấu `-`, tối đa 40 ký tự, không bắt đầu/kết thúc bằng `-` (chuẩn tên tài nguyên K8s).
- URL repository phải bắt đầu bằng `https://`.
- Tên file upload chỉ giữ lại chữ, số, `.`, `_`, `-`; các ký tự khác bị thay bằng `_`.

### Deploy & quản lý ứng dụng ([main.py](main.py))

| Method | Endpoint | Mô tả |
|---|---|---|
| GET | `/api/ai-providers` | Danh sách AI engine và model |
| GET | `/api/open-source-templates` | Danh mục CMS / mã nguồn mở |
| POST | `/api/deploy` | Deploy repo đơn (JSON) |
| POST | `/api/deploy-with-db` | Deploy repo + DB, có thể kèm file backup (form-data) |
| POST | `/api/deploy-fullstack` | Deploy FE + BE + DB (form-data) |
| POST | `/api/deploy-monorepo` | Deploy monorepo (JSON) |
| POST | `/api/deploy-open-source` | Deploy CMS / repo mã nguồn mở với DB trắng (JSON) |
| POST | `/api/deploy-archive` | Deploy từ file nén (form-data) |
| POST | `/api/deploy-archive-with-db` | Deploy file nén + DB (form-data) |
| DELETE | `/api/apps/{app_name}` | Xoá toàn bộ tài nguyên K8s, DB, image, workspace, config, log |
| GET | `/api/logs/{app_name}` | Log build/deploy |
| GET | `/api/pod-logs/{app_name}?component=be\|fe&lines=200` | Log runtime của Pod (tự lấy log lần crash trước nếu cần) |
| GET | `/api/apps/{app_name}/config` | Đọc cấu hình app |
| POST | `/api/apps/{app_name}/config` | Bật/tắt Auto CI/CD (`{"auto_cicd": true}`) |
| POST | `/api/apps/{app_name}/sync-repo` | Đổi/đồng bộ link repo và chủ sở hữu GitHub |
| POST | `/api/apps/{app_name}/redeploy` | Rebuild thủ công (`branch`, `target_component`, `pull_git`) |
| POST | `/api/webhooks/github/{app_name}` | Endpoint nhận GitHub Webhook (push) |
| GET | `/api/apps/{app_name}/history?component=` | Lịch sử revision của Deployment |
| POST | `/api/apps/{app_name}/rollback` | Rollback (`revision: 0` = về bản liền trước) |

Ví dụ deploy repo đơn:

```bash
curl -X POST http://localhost:8080/api/deploy \
  -H "X-API-Key: $VIBE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/my-app.git",
    "app_name": "my-app",
    "app_domain": "my-app.vibe.quyenlt.com",
    "github_token": "ghp_xxx",
    "ai_provider": "agy"
  }'

# Theo dõi tiến trình
curl -H "X-API-Key: $VIBE_API_KEY" http://localhost:8080/api/logs/my-app
```

### File Manager & Git ([file_manager_api.py](file_manager_api.py)) — prefix `/api/project`

| Method | Endpoint | Mô tả |
|---|---|---|
| GET | `/{project_name}/files` | Cây thư mục của workspace |
| GET | `/{project_name}/file?path=...` | Đọc nội dung file |
| PUT | `/{project_name}/file` | Ghi file (`{"path", "content"}`) |
| POST | `/{project_name}/github/push` | Commit và push lên GitHub của chủ token (tự tạo repo private nếu chưa có), chuyển remote sang repo đó, tuỳ chọn tự rebuild |

Đường dẫn file được kiểm tra để không thoát ra ngoài workspace, và không cho đọc/ghi thư mục `.git` (nơi chứa remote URL kèm token).

### AI Debug ([hermes_agent_router.py](hermes_agent_router.py)) — prefix `/api/ai-debug`

| Method | Endpoint | Mô tả |
|---|---|---|
| POST | `/analyze` | Thu thập log build, log Pod, sự kiện K8s, Ingress/Service, file liên quan trong stack trace → AI trả về `diagnosis`, `root_cause`, `actions`. `auto_apply: true` để thực thi luôn. |
| POST | `/{app_name}/execute` | Thực thi danh sách hành động đã được người dùng duyệt |
| POST | `/chat` | Chat tự do với AI về ứng dụng (có lịch sử hội thoại) |

Các hành động được sắp theo nguyên tắc **ưu tiên hạ tầng trước, sửa code sau cùng**:

1. `set_env` — bơm biến môi trường vào Deployment
2. `apply_manifest` — apply YAML (Middleware Traefik, Ingress, Secret…)
3. `restart_deployment`
4. `exec_pod` — chạy lệnh của ứng dụng trong Pod (seed, migrate). Lệnh chứa `kubectl` bị từ chối; lệnh do AI sinh ra không bao giờ được chạy trực tiếp trên VPS.
5. `edit_file` — sửa code (biện pháp cuối cùng)
6. `rebuild`

Nếu có `edit_file` và truyền `github_token`, code đã sửa được tự push lên GitHub.

---

## CI/CD cho ứng dụng người dùng

- Webhook `https://<api-domain>/api/webhooks/github/<app_name>` (sự kiện **push**) được frontend Vibe Hosting tự đăng ký lên repo của người dùng bằng GitHub token của họ khi bật Auto Deploy, kèm secret `GITHUB_WEBHOOK_SECRET`. Frontend và backend phải dùng cùng một giá trị secret; request có chữ ký sai sẽ bị từ chối (`401`).
- Khi có push và `auto_cicd` đang bật, [cicd_core.py](cicd_core.py) sẽ:
  1. Nhận diện loại app (fullstack / monorepo / single) theo thư mục workspace.
  2. Chỉ rebuild component có repo trùng với repo vừa push.
  3. `git fetch` + `git reset --hard origin/<branch>` (tự chọn `main`/`master`).
  4. Build image tag `<user>/<app>:<timestamp>`, push và `kubectl set image` (rolling update).
- **Bảo vệ repo đã fork**: nếu app đã được liên kết với tài khoản GitHub của người dùng, webhook đến từ repo của tác giả gốc sẽ bị bỏ qua.

---

## Cài đặt & vận hành

### Yêu cầu trên VPS
- Python 3.10+
- `git`, `tree`, `docker` (đã `docker login` vào Docker Hub), [`nixpacks`](https://nixpacks.com)
- `kubectl` trỏ tới cụm K3s, có Traefik và [cert-manager](https://cert-manager.io)
- Secret `regcred` trong namespace `default` để pull image từ Docker Hub
- Ít nhất một AI engine: `agy` CLI, `claude` CLI (đã đăng nhập), hoặc Hermes API gateway
- Tuỳ chọn cho file nén: `unrar`, `p7zip-full` (hoặc Python `rarfile`, `py7zr`)

### Cài đặt

```bash
git clone https://github.com/tobi1008/vibe-core.git /opt/vibe-hosting
cd /opt/vibe-hosting
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Cấp chứng chỉ SSL tự động
kubectl apply -f cluster-issuer.yaml

# Secret pull image
kubectl create secret docker-registry regcred \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<user> --docker-password=<token>
```

### Biến môi trường (`.env`)

| Biến | Bắt buộc | Mô tả |
|---|---|---|
| `VIBE_API_KEY` | Có | Khoá mà client phải gửi trong header `X-API-Key`. Tạo bằng `openssl rand -hex 32`. |
| `GITHUB_WEBHOOK_SECRET` | Nên có | Secret dùng để xác minh chữ ký webhook GitHub. Để trống thì webhook không được xác minh. |
| `DOCKER_PASSWORD` | Có (khi xoá app) | Mật khẩu/token Docker Hub, dùng để xoá repository image khi xoá app |
| `HERMES_API_URL` | Không | Mặc định `http://127.0.0.1:8645/v1/chat/completions` |
| `INTERNAL_BEARER_TOKEN` | Không | Bearer token gửi tới Hermes API |
| `AGY_BIN_PATH` | Không | Đường dẫn binary `agy` nếu không nằm trong `PATH` |
| `CLAUDE_BIN_PATH` | Không | Đường dẫn binary `claude` nếu không nằm trong `PATH` |

### Chạy

```bash
# Phát triển
python main.py                      # uvicorn, reload, cổng 8080

# Production (ví dụ)
uvicorn main:app --host 0.0.0.0 --port 8080
```

Trên production, API chạy dưới systemd service tên `vibe-api.service`.

### Thư mục dữ liệu runtime (không commit)

| Đường dẫn | Nội dung |
|---|---|
| `/opt/vibe-hosting/workspaces/` | Mã nguồn đã clone: `<app>`, `<app>-be`, `<app>-fe`, `<app>-monorepo` |
| `/opt/vibe-hosting/logs/` | Log build theo từng app |
| `/opt/vibe-hosting/backups/` | File backup DB tải lên |
| `/opt/vibe-hosting/uploads/` | File nén tải lên |
| `/opt/vibe-hosting/apps_config.json` | "Database" cấu hình các app (repo, db_type, auto_cicd, chủ GitHub…) |

### Tự động deploy chính API này

Push lên nhánh `main` → GitHub Actions SSH vào VPS (cổng 2210) và chạy [update.sh](update.sh): `git stash`, `git pull`, `pip install`, `systemctl restart vibe-api.service`.

Secrets cần cấu hình trong repo: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`.

---

## Lưu ý

- Một số giá trị đang cố định trong code: `DOCKER_USER = "tobi1008"`, namespace `default`, đường dẫn `/opt/vibe-hosting`, CORS `https://vibe.quyenlt.com`. Cần sửa trong [builder_core.py](builder_core.py) và [main.py](main.py) khi triển khai ở môi trường khác.
- `VIBE_API_KEY` cho toàn quyền điều khiển hệ thống (deploy, xoá app, ghi file, apply manifest K8s). Không commit khoá này và không nhúng vào code frontend public; frontend nên gọi API qua backend/proxy của mình hoặc yêu cầu người dùng nhập khoá.
- Log build và lỗi trả về client được tự động che token (`https://***@github.com/...`).
- AI CLI được gọi với quyền của service (thường là `root`); `agy` chạy với `--dangerously-skip-permissions`.
- Mật khẩu database được ghi dạng plain text trong manifest StatefulSet/Deployment (không dùng K8s Secret).
