# Vibe Core: Vibe Hosting API

**Vibe Core** là backend của **Vibe Hosting**, một nền tảng PaaS dùng AI để tự động triển khai ứng dụng web. Người dùng chỉ cần cung cấp link GitHub hoặc tải lên file nén mã nguồn. Hệ thống sẽ phân tích mã nguồn bằng AI, sinh cấu hình build và Kubernetes, đóng gói Docker image, rồi đưa ứng dụng lên cụm **K3s** với tên miền và chứng chỉ SSL tự động.

## Mục lục

- [Tính năng chính](#tính-năng-chính)
- [Kiến trúc hệ thống](#kiến-trúc-hệ-thống)
- [Công nghệ sử dụng](#công-nghệ-sử-dụng)
- [Cấu trúc mã nguồn](#cấu-trúc-mã-nguồn)
- [Các luồng triển khai](#các-luồng-triển-khai)
- [AI Engines](#ai-engines)
- [Bảo mật](#bảo-mật)
- [Tài liệu API](#tài-liệu-api)
- [CI/CD cho ứng dụng người dùng](#cicd-cho-ứng-dụng-người-dùng)
- [Cài đặt và vận hành](#cài-đặt-và-vận-hành)
- [Giới hạn hiện tại và hướng phát triển](#giới-hạn-hiện-tại-và-hướng-phát-triển)

---

## Tính năng chính

- **Triển khai bằng AI:** AI đọc mã nguồn, sinh `nixpacks.toml` cùng manifest Deployment / Service / Ingress, và tự sửa cấu hình khi build lỗi (thử lại tối đa 3 lần).
- **Nhiều kiểu dự án:** repo đơn, fullstack 2 repo (frontend + backend), monorepo, CMS mã nguồn mở (WordPress, Laravel, Ghost) và file nén (`.zip`, `.tar.*`, `.rar`, `.7z`).
- **Database được cấp tự động:** MySQL, MariaDB, PostgreSQL, MongoDB, Redis chạy dưới dạng StatefulSet có ổ lưu trữ riêng. Có thể nạp file backup và tự tiêm thông tin kết nối vào ứng dụng.
- **Tên miền và HTTPS tự động:** Ingress qua Traefik, chứng chỉ Let's Encrypt do cert-manager cấp.
- **CI/CD:** tự build lại và cập nhật ứng dụng (rolling update) mỗi khi người dùng push code lên GitHub.
- **Vận hành:** xem log build và log runtime, lịch sử phiên bản, rollback, chỉnh sửa mã nguồn trực tuyến và push ngược lên GitHub.
- **AI Debug (SRE Agent):** AI thu thập log và sự kiện Kubernetes, chẩn đoán nguyên nhân, đề xuất và thực thi các bước khắc phục.

---

## Kiến trúc hệ thống

```
  Người dùng
      │
      ▼
 Vibe Hosting Frontend (Next.js)  ── đăng nhập, phân quyền theo chủ sở hữu app
      │  REST + X-API-Key (gọi từ server)
      ▼
 ┌──────────────────────────────┐        ┌───────────────────────────────┐
 │  Vibe Core API (FastAPI)     │──AI──▶ │ Antigravity CLI / Claude Code │
 │  main.py + các router        │        │ CLI / Hermes (OpenAI-compat)  │
 └──────────────┬───────────────┘        └───────────────────────────────┘
                │ Background task
                ▼
   git clone ─▶ auto-heal ─▶ AI sinh cấu hình ─▶ nixpacks build
             ─▶ docker push (Docker Hub) ─▶ kubectl apply (K3s)
                                                   │
                     Deployment + Service + Ingress (Traefik, TLS Let's Encrypt)
                     StatefulSet + PVC + Service cho database (nếu có)
```

- Mọi tác vụ triển khai chạy nền, API trả kết quả ngay.
- Tiến trình được ghi vào `logs/<app_name>.log`. Dòng cuối là `DONE`, `ERROR` hoặc `DELETED` để frontend biết trạng thái.
- Frontend là một repo riêng. Mọi lời gọi tới Vibe Core đi qua API route phía server của frontend, nên khoá API không bao giờ xuống trình duyệt.

---

## Công nghệ sử dụng

| Thành phần | Công nghệ |
|---|---|
| API | Python 3.10+, FastAPI, Uvicorn, Pydantic |
| Build image | [Nixpacks](https://nixpacks.com), Docker, Docker Hub |
| Điều phối container | K3s (Kubernetes), Traefik Ingress, [cert-manager](https://cert-manager.io) + Let's Encrypt |
| Database cho ứng dụng | MySQL 8, MariaDB 10.11, PostgreSQL 15, MongoDB 6, Redis 7 |
| AI | Google Antigravity CLI (Gemini Flash), Claude Code CLI, Hermes Agent (LLM mã nguồn mở) |
| Git | GitPython, GitHub REST API, GitHub Webhook |
| Triển khai chính API | GitHub Actions → SSH → systemd |

---

## Cấu trúc mã nguồn

| File | Vai trò |
|---|---|
| [main.py](main.py) | Ứng dụng FastAPI: endpoint triển khai, quản lý app, log, lịch sử, rollback, webhook. |
| [security.py](security.py) | Xác thực API key, kiểm tra dữ liệu đầu vào, xác minh chữ ký webhook, che thông tin nhạy cảm trong log. |
| [builder_core.py](builder_core.py) | Lõi dùng chung: bộ điều phối AI (`call_ai_json`, `call_ai_prompt`), ghi log, triển khai repo đơn, xoá app. |
| [db_builder_core.py](db_builder_core.py) | Triển khai kèm database: sinh manifest StatefulSet/PVC, nhận diện cổng, nạp backup, triển khai fullstack 2 repo. |
| [cms_builder_core.py](cms_builder_core.py) | Triển khai CMS / mã nguồn mở với database trống; tự sinh `wp-config.php`, salt keys, `.env`. |
| [monorepo_builder_core.py](monorepo_builder_core.py) | Triển khai monorepo: tìm thư mục backend/frontend, cấu hình CORS và URL API giữa hai phần. |
| [archive_builder_core.py](archive_builder_core.py) | Triển khai từ file nén; giải nén an toàn (chống Zip Slip / path traversal). |
| [cicd_core.py](cicd_core.py) | CI/CD: cập nhật mã nguồn, build image theo phiên bản, rolling update trên K3s. |
| [file_manager_api.py](file_manager_api.py) | Router `/api/project`: duyệt, đọc, ghi file trong workspace; push lên GitHub của người dùng. |
| [hermes_agent_router.py](hermes_agent_router.py) | Router `/api/ai-debug`: chẩn đoán lỗi bằng AI, thực thi hành động khắc phục, chat với AI. |
| [builder.py](builder.py) | Bản prototype đầu tiên dạng script CLI, giữ lại để tham khảo quá trình phát triển; API không sử dụng. |
| [cluster-issuer.yaml](cluster-issuer.yaml) | `ClusterIssuer` Let's Encrypt cho cert-manager (HTTP-01 qua Traefik). |
| [update.sh](update.sh) | Script cập nhật trên máy chủ: pull code, cài dependency, khởi động lại service. |
| [.github/workflows/deploy.yml](.github/workflows/deploy.yml) | GitHub Actions tự triển khai API khi push lên `main`. |

---

## Các luồng triển khai

### 1. Repo đơn: `POST /api/deploy`

1. Clone repo vào workspace riêng của app.
2. **Auto-heal mạng:** đổi `127.0.0.1` / `localhost` thành `0.0.0.0` để container nhận được traffic từ bên ngoài.
3. Thu thập cấu trúc thư mục và các file cấu hình chính (`package.json`, `requirements.txt`, `composer.json`, `vite.config.*`…) rồi gửi cho AI.
4. AI trả về JSON gồm `nixpacks.toml`, các manifest K3s, và các bản vá cấu hình cần thiết (ví dụ `allowedHosts` cho Vite).
5. Build bằng Nixpacks. Nếu build lỗi, log lỗi được gửi lại cho AI để sửa, **tối đa 3 lần**.
6. Push image lên Docker Hub và `kubectl apply`.

### 2. Repo kèm database: `POST /api/deploy-with-db`

Hệ thống tạo database (`<app>-db`, ổ lưu trữ 1Gi, Service `<app>-db-svc`), chờ database sẵn sàng, nạp file backup nếu có, rồi tiêm thông tin kết nối vào ứng dụng qua biến môi trường (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_URL`, `SPRING_DATASOURCE_*`, `MONGO_URI`…). Khi triển khai lại, mật khẩu database cũ được giữ nguyên.

| `db_type` | Image | Cổng |
|---|---|---|
| `mysql` | `mysql:8.0` | 3306 |
| `mariadb` | `mariadb:10.11` | 3306 |
| `postgresql` | `postgres:15` | 5432 |
| `mongodb` | `mongo:6.0` | 27017 |
| `redis` | `redis:7.2` | 6379 |

Cổng của ứng dụng được nhận diện tự động: PHP → 80; Java Spring Boot → `server.port` hoặc 8080; Node.js → `PORT` trong `.env` hoặc 3000; Python → 8000.

### 3. Fullstack 2 repo: `POST /api/deploy-fullstack`

Frontend và backend dùng chung một database. Backend chạy dưới tên `<app>-be`, frontend dưới tên `<app>-fe`. Frontend được tự cấu hình URL backend (`VITE_API_URL`, `REACT_APP_API_URL`, `NEXT_PUBLIC_API_URL`…).

### 4. Monorepo: `POST /api/deploy-monorepo`

Tự tìm thư mục backend (`backend`, `server`, `api`…) và frontend (`frontend`, `client`, `web`…), hoặc suy ra từ dependency trong `package.json`. Sau đó tạo database trống, cấu hình CORS cho backend và sinh các biến môi trường cần thiết (JWT, CORS, `DATABASE_URL`).

### 5. CMS / mã nguồn mở: `POST /api/deploy-open-source`

Có sẵn các mẫu **WordPress**, **Laravel**, **Ghost**, và **repo tuỳ chọn** (danh sách lấy qua `GET /api/open-source-templates`), luôn đi kèm database trống. Riêng WordPress được tự sinh `wp-config.php`, 8 salt keys và cấu hình HTTPS phía sau reverse proxy.

### 6. File nén: `POST /api/deploy-archive`, `POST /api/deploy-archive-with-db`

Hỗ trợ `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.rar`, `.7z`. File được giải nén an toàn, thư mục gốc lồng nhau được làm phẳng, sau đó đi theo luồng triển khai tương ứng. Ứng dụng tạo từ file nén không có CI/CD tự động.

---

## AI Engines

Mỗi yêu cầu triển khai hoặc debug có thể chọn engine qua tham số `ai_provider`. Nếu một model lỗi hoặc quá thời gian, engine tự chuyển sang model tiếp theo trong danh sách.

| `ai_provider` | Engine | Cách gọi | Model |
|---|---|---|---|
| `agy` (mặc định) | Google Antigravity CLI | CLI trên máy chủ | Gemini Flash 3.7 / 3.8 (5 mức) |
| `claude` | Claude Code CLI | CLI trên máy chủ | Opus, Sonnet, Haiku |
| `hermes` | Hermes Agent | HTTP, API tương thích OpenAI | Các LLM mã nguồn mở |

Danh sách engine và model hiện có được trả về qua `GET /api/ai-providers`.

---

## Bảo mật

| Cơ chế | Mô tả |
|---|---|
| Xác thực API | Mọi endpoint yêu cầu header `X-API-Key`, so khớp theo thời gian hằng (constant-time). Riêng webhook được xác thực bằng chữ ký GitHub. |
| Chữ ký webhook | Kiểm tra `X-Hub-Signature-256` (HMAC-SHA256) với `GITHUB_WEBHOOK_SECRET`. |
| Kiểm tra đầu vào | `app_name` theo chuẩn tên tài nguyên Kubernetes; URL repo bắt buộc `https://`; tên file upload được làm sạch. Các ràng buộc này chặn chèn lệnh shell, chèn tham số vào `git` và path traversal. |
| Thực thi lệnh từ AI | Lệnh do AI đề xuất chỉ được chạy bên trong container của ứng dụng, không bao giờ chạy trên máy chủ. Thao tác Kubernetes chỉ đi qua các hành động có kiểm soát. |
| Bảo vệ thông tin nhạy cảm | GitHub token được che trong log build và thông báo lỗi; File Manager không cho truy cập thư mục `.git`. |
| Giới hạn workspace | Mọi thao tác đọc/ghi file được kiểm tra để không vượt ra ngoài workspace của app. |
| Giải nén an toàn | Chống Zip Slip / path traversal với ZIP và TAR. |

Phân quyền theo người dùng (ai sở hữu app nào) được thực hiện ở tầng frontend. Vibe Core tin tưởng client nắm giữ API key.

---

## Tài liệu API

**Xác thực:** gửi header `X-API-Key: <VIBE_API_KEY>` với mọi request, trừ `/api/webhooks/*`.

| Mã lỗi | Ý nghĩa |
|---|---|
| `400` | Dữ liệu đầu vào không hợp lệ (tên app, URL repo…) |
| `401` | Thiếu hoặc sai API key / chữ ký webhook |
| `503` | Máy chủ chưa cấu hình `VIBE_API_KEY` |

**Ràng buộc `app_name`:** chữ thường, số và dấu `-`; tối đa 40 ký tự; không bắt đầu hay kết thúc bằng `-`.

### Triển khai và quản lý ứng dụng

| Method | Endpoint | Mô tả |
|---|---|---|
| GET | `/api/ai-providers` | Danh sách AI engine và model |
| GET | `/api/open-source-templates` | Danh mục CMS / mã nguồn mở |
| POST | `/api/deploy` | Triển khai repo đơn (JSON) |
| POST | `/api/deploy-with-db` | Triển khai repo kèm database, có thể kèm file backup (form-data) |
| POST | `/api/deploy-fullstack` | Triển khai frontend + backend + database (form-data) |
| POST | `/api/deploy-monorepo` | Triển khai monorepo (JSON) |
| POST | `/api/deploy-open-source` | Triển khai CMS / mã nguồn mở với database trống (JSON) |
| POST | `/api/deploy-archive` | Triển khai từ file nén (form-data) |
| POST | `/api/deploy-archive-with-db` | Triển khai file nén kèm database (form-data) |
| DELETE | `/api/apps/{app_name}` | Xoá toàn bộ tài nguyên của app (K3s, database, image, workspace, cấu hình, log) |
| GET | `/api/logs/{app_name}` | Log build / triển khai |
| GET | `/api/pod-logs/{app_name}?component=be\|fe&lines=200` | Log runtime của container (kèm log lần crash trước nếu có) |
| GET | `/api/apps/{app_name}/config` | Đọc cấu hình app |
| POST | `/api/apps/{app_name}/config` | Bật / tắt CI/CD tự động |
| POST | `/api/apps/{app_name}/sync-repo` | Cập nhật link repo và chủ sở hữu GitHub |
| POST | `/api/apps/{app_name}/redeploy` | Build lại thủ công (`branch`, `target_component`, `pull_git`) |
| GET | `/api/apps/{app_name}/history?component=` | Lịch sử phiên bản triển khai |
| POST | `/api/apps/{app_name}/rollback` | Quay về phiên bản cũ (`revision: 0` = bản liền trước) |
| POST | `/api/webhooks/github/{app_name}` | Nhận GitHub Webhook (sự kiện push) |

Ví dụ triển khai một repo:

```bash
curl -X POST http://localhost:8080/api/deploy \
  -H "X-API-Key: $VIBE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/<user>/<repo>.git",
    "app_name": "my-app",
    "app_domain": "my-app.example.com",
    "ai_provider": "agy"
  }'

# Theo dõi tiến trình
curl -H "X-API-Key: $VIBE_API_KEY" http://localhost:8080/api/logs/my-app
```

### File Manager và Git (prefix `/api/project`)

| Method | Endpoint | Mô tả |
|---|---|---|
| GET | `/{project_name}/files` | Cây thư mục của workspace |
| GET | `/{project_name}/file?path=...` | Đọc nội dung file |
| PUT | `/{project_name}/file` | Ghi file (`{"path", "content"}`) |
| POST | `/{project_name}/github/push` | Commit và push lên GitHub của người dùng (tự tạo repo private nếu chưa có), tuỳ chọn build lại ngay |

### AI Debug (prefix `/api/ai-debug`)

| Method | Endpoint | Mô tả |
|---|---|---|
| POST | `/analyze` | Thu thập log build, log runtime, sự kiện K8s, Ingress/Service và các file xuất hiện trong stack trace; AI trả về chẩn đoán, nguyên nhân gốc và danh sách hành động. Gửi `auto_apply: true` để thực thi luôn. |
| POST | `/{app_name}/execute` | Thực thi các hành động đã được người dùng duyệt |
| POST | `/chat` | Hỏi đáp với AI về ứng dụng, có lưu lịch sử hội thoại |

Các hành động khắc phục được ưu tiên theo nguyên tắc **sửa hạ tầng trước, sửa mã nguồn sau cùng**:

1. `set_env`: cập nhật biến môi trường của Deployment
2. `apply_manifest`: áp dụng manifest Kubernetes (Traefik Middleware, Ingress, Secret…)
3. `restart_deployment`: khởi động lại ứng dụng
4. `exec_pod`: chạy lệnh của ứng dụng bên trong container (seed, migrate…)
5. `edit_file`: sửa mã nguồn, chỉ dùng khi không còn cách nào khác
6. `rebuild`: build lại sau khi sửa mã nguồn

Khi có `edit_file` và người dùng cung cấp GitHub token, bản sửa được tự động push lên repo của họ.

---

## CI/CD cho ứng dụng người dùng

1. Khi người dùng bật Auto Deploy, frontend dùng GitHub token của họ để đăng ký webhook `https://<api-domain>/api/webhooks/github/<app_name>` (sự kiện **push**) lên repo của họ, kèm secret dùng chung `GITHUB_WEBHOOK_SECRET`.
2. Khi có push, Vibe Core xác minh chữ ký rồi gọi [cicd_core.py](cicd_core.py):
   - Nhận diện kiểu app (đơn / fullstack / monorepo) và chỉ build lại phần có repo vừa được push.
   - Đồng bộ mã nguồn theo nhánh `main` hoặc `master`.
   - Build image gắn tag theo thời điểm build (`<user>/<app>:<timestamp>`), push và rolling update trên K3s.
3. **Bảo vệ repo đã fork:** nếu app đã chuyển sang repo trên tài khoản của người dùng, webhook đến từ repo của tác giả gốc sẽ bị bỏ qua.

---

## Cài đặt và vận hành

### Yêu cầu

- Máy chủ Linux có Python 3.10+, `git`, `tree`, Docker (đã đăng nhập Docker Hub) và Nixpacks
- Cụm K3s có Traefik và cert-manager; `kubectl` đã được cấu hình để truy cập cụm
- Ít nhất một AI engine: Antigravity CLI (`agy`), Claude Code CLI (`claude`), hoặc một Hermes API gateway
- Tuỳ chọn, để giải nén `.rar` / `.7z`: `unrar`, `p7zip-full`

### Cài đặt

```bash
git clone https://github.com/tobi1008/vibe-core.git /opt/vibe-hosting
cd /opt/vibe-hosting
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Issuer cấp chứng chỉ Let's Encrypt (sửa email trong file trước khi apply)
kubectl apply -f cluster-issuer.yaml

# Secret để K3s pull image từ Docker Hub
kubectl create secret docker-registry regcred \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<docker-user> --docker-password=<docker-token>
```

### Biến môi trường

Tạo file `.env` trong thư mục gốc của dự án. File này đã nằm trong `.gitignore`.

| Biến | Bắt buộc | Mô tả |
|---|---|---|
| `VIBE_API_KEY` | Có | Khoá API mà client phải gửi trong header `X-API-Key`. Tạo bằng `openssl rand -hex 32`. |
| `GITHUB_WEBHOOK_SECRET` | Khuyến nghị | Secret để xác minh webhook GitHub; phải trùng với giá trị cấu hình ở frontend. Nếu để trống, webhook không được xác minh chữ ký. |
| `DOCKER_PASSWORD` | Khuyến nghị | Token Docker Hub, dùng để xoá image khi xoá app. |
| `HERMES_API_URL` | Không | Endpoint của Hermes. Mặc định `http://127.0.0.1:8645/v1/chat/completions`. |
| `INTERNAL_BEARER_TOKEN` | Không | Bearer token gửi tới Hermes. |
| `AGY_BIN_PATH`, `CLAUDE_BIN_PATH` | Không | Đường dẫn tới CLI nếu không nằm trong `PATH`. |

### Chạy

```bash
# Môi trường phát triển (tự reload khi sửa code, cổng 8080)
python main.py

# Môi trường production
uvicorn main:app --host 0.0.0.0 --port 8080
```

Trên production, API chạy dưới dạng systemd service `vibe-api.service`.

### Dữ liệu lúc chạy

Các thư mục sau được tạo tự động trong `/opt/vibe-hosting` và không được đưa vào Git:

| Đường dẫn | Nội dung |
|---|---|
| `workspaces/` | Mã nguồn của từng app (`<app>`, `<app>-be`, `<app>-fe`, `<app>-monorepo`) |
| `logs/` | Log build của từng app |
| `backups/` | File backup database được tải lên |
| `uploads/` | File nén được tải lên |
| `apps_config.json` | Cấu hình của các app (repo, loại database, trạng thái CI/CD…) |

### Tự động triển khai API

Mỗi lần push lên nhánh `main`, GitHub Actions kết nối SSH tới máy chủ và chạy [update.sh](update.sh) để cập nhật mã nguồn, cài dependency và khởi động lại service. Cần cấu hình các secret `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` trong repository.

### Tuỳ chỉnh cho môi trường khác

Một số giá trị đang được khai báo trực tiếp trong mã nguồn và cần chỉnh lại khi triển khai ở hạ tầng khác:

| Giá trị | Vị trí |
|---|---|
| Tài khoản Docker Hub (`DOCKER_USER`) | [builder_core.py](builder_core.py) |
| Namespace Kubernetes (`K8S_NAMESPACE`) | [builder_core.py](builder_core.py) |
| Thư mục gốc `/opt/vibe-hosting` | [builder_core.py](builder_core.py), [main.py](main.py) |
| Domain frontend được phép gọi API (CORS) | [main.py](main.py) |
| Email đăng ký Let's Encrypt | [cluster-issuer.yaml](cluster-issuer.yaml) |

---

## Giới hạn hiện tại và hướng phát triển

| Giới hạn | Hướng cải thiện |
|---|---|
| Cấu hình app được lưu trong file JSON (`apps_config.json`) trên một máy chủ. | Chuyển sang cơ sở dữ liệu để hỗ trợ nhiều instance API. |
| Mật khẩu database của ứng dụng được ghi trực tiếp trong manifest. | Lưu bằng Kubernetes Secret. |
| AI CLI chạy với quyền của service trên máy chủ. | Chạy AI trong container cô lập, quyền tối thiểu. |
| Các giá trị hạ tầng (Docker Hub, namespace, đường dẫn) đang cố định trong mã nguồn. | Đưa toàn bộ vào biến môi trường. |
| Chưa có bộ kiểm thử tự động. | Bổ sung unit test và integration test vào CI. |
