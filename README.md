# Vibe Core: Vibe Hosting API

**Vibe Core** là backend của **Vibe Hosting**, một nền tảng hosting (PaaS) dùng AI để tự động triển khai ứng dụng web lên cụm **K3s**. Người dùng chỉ cần đưa link GitHub hoặc tải lên file nén mã nguồn. AI phân tích mã nguồn, sinh cấu hình build và Kubernetes, hệ thống đóng gói Docker image rồi đưa ứng dụng lên với tên miền và chứng chỉ SSL tự động. Sau khi chạy, AI tiếp tục kiểm tra, chẩn đoán và sửa lỗi; người dùng có thể làm việc với hệ thống qua web hoặc qua **Hermes**, quản gia trên Telegram.

Frontend (Next.js, `vibe.quyenlt.com`) là repo riêng: [tobi1008/vibehosting-FE](https://github.com/tobi1008/vibehosting-FE).

## Mục lục

- [Tính năng chính](#tính-năng-chính)
- [Kiến trúc hệ thống](#kiến-trúc-hệ-thống)
- [Công nghệ sử dụng](#công-nghệ-sử-dụng)
- [Cấu trúc mã nguồn](#cấu-trúc-mã-nguồn)
- [Các luồng triển khai](#các-luồng-triển-khai)
- [AI Engines](#ai-engines)
- [Gói hosting và tài nguyên](#gói-hosting-và-tài-nguyên)
- [AI Debug](#ai-debug)
- [Hermes: quản gia qua Telegram](#hermes-quản-gia-qua-telegram)
- [Bảo mật và cô lập](#bảo-mật-và-cô-lập)
- [Dọn dẹp tài nguyên](#dọn-dẹp-tài-nguyên)
- [Tài liệu API](#tài-liệu-api)
- [CI/CD cho ứng dụng người dùng](#cicd-cho-ứng-dụng-người-dùng)
- [Cài đặt và vận hành](#cài-đặt-và-vận-hành)
- [Kiểm thử](#kiểm-thử)

---

## Tính năng chính

- **Triển khai bằng AI:** AI đọc mã nguồn, sinh `nixpacks.toml` cùng manifest Deployment / Service / Ingress, tự sửa cấu hình khi build lỗi (thử lại tối đa 3 lần).
- **Nhiều kiểu dự án:** repo đơn, repo kèm database, fullstack 2 repo, monorepo, CMS mã nguồn mở (WordPress, Laravel, Ghost) và file nén (`.zip`, `.tar.*`, `.rar`, `.7z`).
- **Database tự động:** MySQL, MariaDB, PostgreSQL, MongoDB, Redis chạy dạng StatefulSet có ổ riêng; nạp file backup và tiêm thông tin kết nối vào ứng dụng.
- **Tên miền và HTTPS tự động:** Ingress qua Traefik, chứng chỉ Let's Encrypt do cert-manager cấp.
- **CI/CD:** tự build lại và rolling update khi người dùng push code lên GitHub.
- **Gói hosting:** admin tạo gói (tổng CPU, RAM, dung lượng database, số project); người dùng chia tài nguyên của gói cho từng project.
- **Chọn AI:** Antigravity (Gemini 3.8 Flash), Gemini 4 Hydrogen, Claude Code, hoặc API key riêng của người dùng (OpenAI, Anthropic, Gemini, API tương thích OpenAI).
- **AI Debug:** chẩn đoán từ log và sự kiện K3s; chế độ **Agent** đọc và sửa mã nguồn, file K3s của app trong hộp cát, người dùng xem diff rồi bấm duyệt mới áp dụng.
- **Hermes trên Telegram:** hỏi tình trạng, xem log, chẩn đoán, restart/rollback/build lại, chạy AI Debug; tự cảnh báo khi app lỗi và gửi báo cáo hằng ngày.
- **Cô lập người dùng:** mỗi người chỉ thấy và thao tác app của mình; mạng giữa các app được cô lập bằng NetworkPolicy; AI không có quyền vào máy chủ.
- **Usage:** đo CPU, RAM, dung lượng database thật, ổ tạm của từng app; kiểm tra HTTPS và hạn chứng chỉ của mọi tên miền.
- **Tự dọn dẹp:** build cache và image cũ trên máy build, image của app đã xóa trên node K3s, rác trên Docker Hub.

---

## Kiến trúc hệ thống

```
  Người dùng ──── Web (Next.js, Vercel) ──────────────┐        Telegram ── Hermes Butler
                   đăng nhập, gói hosting, Usage,      │                    (trong Vibe Core)
                   phân quyền theo chủ app             │ REST + X-API-Key
                                                       ▼
 ┌─────────────────────────────── Máy chủ backend ────────────────────────────────┐
 │  Vibe Core API (FastAPI, systemd vibe-api)                                      │
 │   ├─ luồng triển khai (chạy nền) ──▶ git clone ─▶ AI sinh cấu hình ─▶ nixpacks  │
 │   │                                  build ─▶ docker push ─▶ kubectl apply      │
 │   ├─ AI Debug / Agent ──▶ AI CLI chạy bằng user vibe-ai trong hộp cát           │
 │   ├─ Hermes Butler (Telegram), Hermes Monitor (giám sát), Hermes Verifier       │
 │   └─ dọn Docker định kỳ, áp NetworkPolicy, LimitRange                           │
 │  Hermes gateway (hermes-proxy, 127.0.0.1:8645): LLM miễn phí cho Hermes         │
 └──────────────────────────────────┬──────────────────────────────────────────────┘
                                    │ kubectl (kubeconfig chỉ root đọc được)
                                    ▼
 ┌──────────────────────────────── Cụm K3s ───────────────────────────────────────┐
 │  Mỗi app: Deployment + Service + Ingress (Traefik, TLS Let's Encrypt)           │
 │           StatefulSet + PVC + Service cho database (nếu có)                     │
 │           NetworkPolicy vibe-app-<app>, limits theo gói                          │
 └─────────────────────────────────────────────────────────────────────────────────┘
```

- Mọi tác vụ triển khai chạy nền, API trả kết quả ngay. Tiến trình ghi vào `logs/<app>.log`; dòng cuối là `DONE`, `ERROR` hoặc `DELETED` để frontend biết trạng thái.
- Mọi lời gọi từ web đi qua API route phía server của frontend, nên `VIBE_API_KEY` không bao giờ xuống trình duyệt. Riêng upload file nén dùng token ngắn hạn để trình duyệt gửi thẳng tới Vibe Core.
- Frontend giữ dữ liệu người dùng (Prisma + Postgres): tài khoản, project, gói hosting, phân bổ tài nguyên, nhật ký hoạt động. Vibe Core hỏi frontend qua `GET /api/internal/telegram/directory` để biết ai sở hữu app nào.

---

## Công nghệ sử dụng

| Thành phần | Công nghệ |
|---|---|
| API | Python 3.11, FastAPI, Uvicorn, Pydantic |
| Build image | [Nixpacks](https://nixpacks.com), Docker, Docker Hub |
| Điều phối container | K3s, Traefik Ingress, [cert-manager](https://cert-manager.io) + Let's Encrypt, NetworkPolicy (kube-router) |
| Database cho ứng dụng | MySQL 8, MariaDB 10.11, PostgreSQL 15, MongoDB 6, Redis 7 |
| AI | Google Antigravity CLI (Gemini 3.8 Flash, Gemini 4 Hydrogen), Claude Code CLI, Hermes Agent (LLM miễn phí qua gateway), API key riêng của người dùng |
| Telegram | Bot API (long polling) |
| Git | GitPython, GitHub REST API, GitHub Webhook |
| Mã hóa | `cryptography` (Fernet) cho API key AI của người dùng |
| Triển khai chính API | GitHub Actions → SSH → [update.sh](update.sh) → systemd |

---

## Cấu trúc mã nguồn

**Lõi và triển khai**

| File | Vai trò |
|---|---|
| [main.py](main.py) | Ứng dụng FastAPI: endpoint triển khai, quản lý app, log, lịch sử, rollback, webhook, gói, usage; khởi động các tác vụ nền. |
| [security.py](security.py) | Xác thực API key, token upload, kiểm tra đầu vào, chữ ký webhook, che thông tin nhạy cảm trong log. |
| [builder_core.py](builder_core.py) | Lõi dùng chung: điều phối AI (`call_ai_json`, `call_ai_prompt`), ghi log, `run_cmd`, triển khai repo đơn, xóa app. |
| [db_builder_core.py](db_builder_core.py) | Triển khai kèm database: StatefulSet/PVC, nhận diện cổng, nạp backup, fullstack 2 repo. |
| [cms_builder_core.py](cms_builder_core.py) | CMS / mã nguồn mở với database trống; tự sinh `wp-config.php`, salt keys, `.env`. |
| [monorepo_builder_core.py](monorepo_builder_core.py) | Monorepo: tìm backend/frontend, cấu hình CORS và URL API. |
| [archive_builder_core.py](archive_builder_core.py) | Triển khai từ file nén; giải nén an toàn (chống Zip Slip). |
| [cicd_core.py](cicd_core.py) | CI/CD: cập nhật mã nguồn, build image theo phiên bản, rolling update. |
| [file_manager_api.py](file_manager_api.py) | Router `/api/project`: duyệt, đọc, ghi file trong workspace; push lên GitHub. |
| [builder.py](builder.py) | Bản prototype đầu tiên dạng script CLI, giữ lại để tham khảo; API không dùng. |

**AI**

| File | Vai trò |
|---|---|
| [ai_sandbox.py](ai_sandbox.py) | Chạy AI CLI (agy, claude) bằng user `vibe-ai` trong thư mục trống, môi trường đã lọc bí mật. |
| [ai_agent_session.py](ai_agent_session.py) | AI Debug chế độ Agent: model sửa code và file K3s trên bản sao mã nguồn của đúng app; tạo diff, kiểm tra, áp dụng khi được duyệt. |
| [ai_credentials.py](ai_credentials.py), [ai_credentials_api.py](ai_credentials_api.py) | API key AI riêng của người dùng: lưu mã hóa, chỉ dùng cho app của chính chủ key. |
| [byo_ai.py](byo_ai.py) | Gọi OpenAI / Anthropic / Gemini / API tương thích OpenAI bằng key của người dùng. |
| [hermes_agent_router.py](hermes_agent_router.py) | Router `/api/ai-debug`: chẩn đoán, thực thi hành động khắc phục, chat, phiên Agent. |
| [hermes_verifier.py](hermes_verifier.py) | Kiểm tra tổng thể sau mỗi lần triển khai (Pod, log, sự kiện) trước khi báo `DONE`. |

**Hermes (Telegram, giám sát)**

| File | Vai trò |
|---|---|
| [hermes_butler.py](hermes_butler.py) | Bot Telegram: vòng lặp tác tử, lệnh nhanh, nút duyệt, chạy AI Debug, `/ai`. |
| [hermes_tools.py](hermes_tools.py) | Các công cụ Hermes được gọi (trạng thái, log, HTTPS, lịch sử, restart, rollback, AI Debug, gói…). |
| [hermes_prefs.py](hermes_prefs.py) | Model AI mỗi người dùng chọn cho Hermes (`/ai`). |
| [hermes_monitor.py](hermes_monitor.py) | Giám sát tự động, cảnh báo, báo cáo hằng ngày. |
| [telegram_api.py](telegram_api.py), [telegram_client.py](telegram_client.py), [telegram_links.py](telegram_links.py) | API cấu hình bot và liên kết tài khoản, gọi Bot API, lưu liên kết chat ↔ tài khoản. |
| [user_directory.py](user_directory.py) | Tra danh bạ người dùng, gói và chủ sở hữu app từ frontend (cache ngắn). |

**Cô lập, tài nguyên, dọn dẹp**

| File | Vai trò |
|---|---|
| [app_scope.py](app_scope.py) | Phạm vi tài nguyên K3s của một app; kiểm tra manifest do AI đề xuất. |
| [package_limits.py](package_limits.py) | Ghi đè CPU/RAM/dung lượng database theo phân bổ vào mọi manifest trước khi apply; LimitRange mặc định. |
| [network_policy.py](network_policy.py) | Cô lập mạng giữa các app bằng NetworkPolicy. |
| [usage.py](usage.py) | Đo CPU/RAM (kubelet), dung lượng database thật (`du` trong Pod), ổ tạm. |
| [docker_janitor.py](docker_janitor.py) | Dọn build cache và image cũ trên máy build. |
| [node_image_cleanup.py](node_image_cleanup.py) | Xóa image của app đã xóa trên node K3s (Job ngắn dùng crictl). |
| [dockerhub_cleanup.py](dockerhub_cleanup.py) | Dọn repo mồ côi và tag cũ trên Docker Hub. |

**Khác:** [cluster-issuer.yaml](cluster-issuer.yaml) (ClusterIssuer Let's Encrypt), [update.sh](update.sh) (cập nhật trên máy chủ), [.github/workflows/deploy.yml](.github/workflows/deploy.yml) (test rồi tự triển khai khi push `main`), [tests/](tests) (bộ kiểm thử).

---

## Các luồng triển khai

### 1. Repo đơn: `POST /api/deploy`

1. Clone repo vào workspace riêng của app.
2. **Auto-heal mạng:** đổi `127.0.0.1` / `localhost` thành `0.0.0.0` để container nhận traffic.
3. Thu thập cây thư mục và các file cấu hình chính (`package.json`, `requirements.txt`, `composer.json`, `vite.config.*`…) đưa vào prompt.
4. AI trả về JSON gồm `nixpacks.toml`, manifest K3s và các bản vá cấu hình (ví dụ `allowedHosts` cho Vite).
5. Build bằng Nixpacks; lỗi thì gửi log cho AI sửa, **tối đa 3 lần**.
6. Push image lên Docker Hub. Trước mỗi `kubectl apply`, Vibe Core tạo NetworkPolicy cho app và ghi đè giới hạn tài nguyên theo phân bổ.
7. **Hermes Verifier** kiểm tra Pod, log, sự kiện; chỉ báo `DONE` khi app thật sự chạy.

### 2. Repo kèm database: `POST /api/deploy-with-db`

Tạo database `<app>-db` (ổ lưu trữ theo phân bổ, Service `<app>-db-svc`), chờ sẵn sàng, nạp file backup nếu có, rồi tiêm thông tin kết nối qua biến môi trường (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_URL`, `SPRING_DATASOURCE_*`, `MONGO_URI`…). Triển khai lại giữ nguyên mật khẩu database cũ.

| `db_type` | Image | Cổng |
|---|---|---|
| `mysql` | `mysql:8.0` | 3306 |
| `mariadb` | `mariadb:10.11` | 3306 |
| `postgresql` | `postgres:15` | 5432 |
| `mongodb` | `mongo:6.0` | 27017 |
| `redis` | `redis:7.2` | 6379 |

Cổng ứng dụng được nhận diện tự động: PHP → 80; Spring Boot → `server.port` hoặc 8080; Node.js → `PORT` trong `.env` hoặc 3000; Python → 8000.

### 3. Fullstack 2 repo: `POST /api/deploy-fullstack`

Frontend (`<app>-fe`) và backend (`<app>-be`) dùng chung một database; frontend được tự cấu hình URL backend (`VITE_API_URL`, `REACT_APP_API_URL`, `NEXT_PUBLIC_API_URL`…).

### 4. Monorepo: `POST /api/deploy-monorepo`

Tự tìm thư mục backend (`backend`, `server`, `api`…) và frontend (`frontend`, `client`, `web`…), tạo database trống, cấu hình CORS và các biến môi trường cần thiết (JWT, `DATABASE_URL`).

### 5. CMS / mã nguồn mở: `POST /api/deploy-open-source`

Mẫu **WordPress**, **Laravel**, **Ghost** hoặc repo tùy chọn (`GET /api/open-source-templates`), luôn kèm database trống. WordPress được tự sinh `wp-config.php`, 8 salt keys và cấu hình HTTPS sau reverse proxy.

### 6. File nén: `POST /api/deploy-archive`, `POST /api/deploy-archive-with-db`

Hỗ trợ `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.rar`, `.7z`. Giải nén an toàn, làm phẳng thư mục gốc lồng nhau, rồi đi theo luồng tương ứng. App từ file nén không có CI/CD tự động. Trình duyệt gửi file thẳng tới Vibe Core bằng token ngắn hạn; giới hạn tài nguyên và chủ app đã được frontend đăng ký trước khi cấp token.

Mọi luồng đều lưu tên miền của app trong `apps_config.json` (thiếu thì Hermes đọc từ Ingress trên K3s).

---

## AI Engines

Mỗi yêu cầu triển khai hay AI Debug chọn engine qua `ai_provider`. Model lỗi hoặc quá thời gian thì tự chuyển sang model kế tiếp.

| `ai_provider` | Engine | Model (thứ tự thử) |
|---|---|---|
| `agy` (mặc định) | Google Antigravity CLI | `gemini-3.8-flash-low` → `-medium` → `-high` |
| `hydrogen` | Gemini 4 Hydrogen qua Antigravity CLI | `gemini-4-hydrogen-medium` → `-high` → `-low` → Gemini 3.8 Flash |
| `claude` | Claude Code CLI | Opus → Sonnet → Haiku |
| `hermes` | Hermes gateway (HTTP, tương thích OpenAI) | Các LLM miễn phí (`HERMES_MODELS`) – dùng cho Hermes Butler, Verifier |
| `byo:<id>` | API key riêng của người dùng | Model người dùng nhập (OpenAI, Anthropic, Gemini, OpenAI-compatible) |

`GET /api/ai-providers` trả danh sách engine; frontend nối thêm các key riêng của người dùng. AI CLI luôn chạy bằng user `vibe-ai` (xem [Bảo mật và cô lập](#bảo-mật-và-cô-lập)).

**Key AI riêng** (`/api/ai-credentials`): key được gọi thử trước khi lưu, lưu ở `ai_credentials.json` (mode 600) mã hóa Fernet bằng `AI_KEYS_SECRET` (không đặt thì dẫn xuất từ `VIBE_API_KEY`), chỉ trả về 4 ký tự cuối. Engine `byo:<id>` chỉ được chấp nhận khi key thuộc **chủ app** do frontend đăng ký (`POST /api/apps/{app}/owner`), không lấy user_id từ trình duyệt. Model gọi qua key riêng chỉ nhận prompt và trả văn bản.

---

## Gói hosting và tài nguyên

- Gói do admin tạo trên web là **tổng** tài nguyên của một người dùng: số project, CPU, RAM, dung lượng database, có được dùng database hay không.
- Khi tạo project, người dùng chọn mức cho app (Nhỏ 250m/256Mi, Vừa 500m/512Mi, Lớn 1 vCPU/1Gi hoặc tùy chỉnh) trong phần còn lại của gói; đổi lại được ở tab Cài đặt của project. Admin không bị giới hạn.
- Frontend gửi phần đã phân bổ qua `POST /api/apps/{app}/resources` `{cpu, memory, db_storage, allow_database, parts}`; `parts` là số thành phần của app (web/backend/frontend/database) để chia đều cho từng container.
- Trước mỗi `kubectl apply -f` (trong `builder_core.run_cmd`), limits của mọi container trong Deployment/StatefulSet bị ghi đè theo phân bổ (requests = 1/4 limits); PVC mới nhận dung lượng phân bổ (PVC đã có giữ nguyên vì local-path không đổi được kích thước). Manifest do AI sinh hay Hermes đề xuất đều không vượt được.
- Gói không có database thì mọi luồng có database trả 403.
- `LimitRange` `vibe-default-limits` (mặc định 1 vCPU / 1Gi) là lưới an toàn cho container không khai báo limits.
- `POST /api/usage` đo CPU/RAM từ kubelet của mọi node, dung lượng database thật bằng `du` trong Pod (cache 5 phút; không dùng số của kubelet vì với local-path nó báo dung lượng cả ổ), ổ tạm và từng Pod; admin kèm tài nguyên node. App có container không khai báo limits được đánh dấu `cpu_unlimited` / `memory_unlimited`.

---

## AI Debug

Router `/api/ai-debug`, dùng trên web (tab AI Debug của project) và qua Hermes.

- **Chẩn đoán** (`POST /analyze`): thu thập log build, log runtime (kèm lần crash trước), sự kiện K3s, Ingress/Service đã lọc theo app và các file xuất hiện trong stack trace; AI trả về chẩn đoán, nguyên nhân gốc và danh sách hành động. Người dùng duyệt rồi `POST /{app}/execute`.
- **Chat** (`POST /chat`): hỏi đáp về app, AI đọc lại log mỗi lượt.
- **Chế độ Agent** (`POST /agent`, `GET /agent/{id}`, `POST /agent/{id}/apply`): xem [bên dưới](#chế-độ-agent).

Hành động khắc phục được ưu tiên **sửa hạ tầng trước, sửa mã nguồn sau cùng**: `set_env` → `apply_manifest` (đã kiểm tra bằng `app_scope` và giới hạn gói) → `restart_deployment` → `exec_pod` (chỉ lệnh của app, trong container) → `edit_file` → `rebuild`. Khi có `edit_file` và người dùng cung cấp GitHub token, bản sửa được push lên repo của họ.

### Chế độ Agent

Model (Antigravity, Gemini 4 Hydrogen hoặc Claude Code) được **đọc, sửa file và chạy lệnh** (build thử, test) nhưng chỉ trên **bản sao** mã nguồn của đúng app:

1. Vibe Core sao chép workspace của app (bỏ `.git`, `node_modules`, `build`…) vào `/home/vibe-ai/jobs/<id>/project`, chạy CLI bằng `sudo -n -u vibe-ai env -i …` với môi trường tối giản (không token, `KUBECONFIG=/dev/null`). Tối đa 10 phút, mỗi app một phiên một lúc.
2. Xong phiên, Vibe Core so bản sao với mã thật để lấy diff các file thêm/sửa/xóa. Manifest `k8s/*.yaml` bị chặn nếu vi phạm phạm vi app hoặc vượt phân bổ; file nhị phân và xóa file không áp dụng tự động.
3. Người dùng xem diff trên web (hoặc Telegram) và bấm **Áp dụng**: Vibe Core ghi file vào mã thật (commit git local), apply manifest hợp lệ, build lại nếu có sửa code. Bản sao luôn bị xóa sau phiên.

Agent không bao giờ chạy `kubectl` hay đụng tới cụm; chỉ Vibe Core apply sau khi người dùng duyệt.

---

## Hermes: quản gia qua Telegram

Một bot chung cho cả hệ thống: admin tạo bot với @BotFather và dán token ở trang Integrations trên web (lưu `telegram_config.json`, quyền 600, ưu tiên hơn `TELEGRAM_BOT_TOKEN`; bot nhận token mới trong ~30 giây). Mỗi người dùng liên kết Telegram của mình bằng mã dùng một lần (hết hạn sau 10 phút). Bot chỉ phục vụ chat riêng.

**Lệnh nhanh:** `/apps`, `/goi`, `/status`, `/logs`, `/check`, `/buildlog`, `/events`, `/history`, `/report`, `/debug`, `/aidebug <app> [hydrogen|agy|claude] <yêu cầu>`, `/restart`, `/rollback`, `/redeploy`, `/ai`, `/reset`, `/unlink`, `/help`. Ngoài ra nhắn tự nhiên bằng tiếng Việt.

- **Vòng lặp tác tử:** Vibe Core gửi cho model danh sách công cụ và app của người dùng; model trả JSON chọn công cụ hoặc câu trả lời; tối đa 6 bước mỗi tin nhắn.
- **Chọn model** (`/ai`, `hermes_prefs.py`): Hermes miễn phí qua gateway (mặc định), Gemini 3.8 Flash, Gemini 4 Hydrogen, Claude Code hoặc key AI riêng. Prompt ghi đúng tên model đang chạy để Hermes không trả lời sai khi được hỏi.
- **AI Debug qua Telegram:** nhắn "dùng AI Debug với Gemini 4 Hydrogen sửa lỗi…" hoặc `/aidebug`; Hermes chạy phiên Agent, xong gửi tóm tắt, danh sách file, nút **Xem diff** và nút **Duyệt** để áp dụng.
- **Duyệt thao tác:** restart, rollback, build lại, hành động khắc phục và áp dụng AI Debug chỉ chạy khi người dùng bấm **Duyệt**; chỉ đúng chat đã nhận đề xuất mới bấm được, hết hạn sau 30 phút, quyền được kiểm tra lại lúc thực thi.
- **Trung thực:** Hermes chỉ nói đã làm khi có kết quả công cụ hoặc dòng `[Hệ thống]` ghi kết quả thật; không có công cụ tạo/xóa app hay đổi tên miền.
- **Phân quyền** (`user_directory.py`): người dùng chỉ thấy và thao tác app của mình; admin thao tác được mọi app (`/apps` ghi rõ chủ của app người khác). Không tra được danh bạ thì từ chối.
- **Giám sát** (`hermes_monitor.py`): mỗi `MONITOR_INTERVAL` giây kiểm tra Pod (CrashLoopBackOff, chưa Ready, restart tăng), HTTPS và hạn chứng chỉ, ổ đĩa/RAM/CPU của máy chủ và node K3s. Cảnh báo khi lỗi lặp lại 2 lần liên tiếp, báo khi hồi phục. Cảnh báo và báo cáo hằng ngày của app chỉ gửi cho **chủ app**; app không có chủ và tài nguyên VPS gửi admin. Admin có nút gửi thử báo cáo trên web. Phần giám sát chạy theo luật, không gọi LLM.

---

## Bảo mật và cô lập

| Lớp | Cơ chế |
|---|---|
| Xác thực API | Mọi endpoint yêu cầu `X-API-Key` (so khớp thời gian hằng). Webhook xác thực bằng chữ ký GitHub (`X-Hub-Signature-256`). Upload file nén dùng `X-Upload-Token` ngắn hạn ký HMAC, gắn với một `app_name`. |
| Kiểm tra đầu vào | `app_name` theo chuẩn tên tài nguyên Kubernetes; URL repo bắt buộc `https://`; tên file upload được làm sạch; tên miền kiểm tra định dạng. Chặn chèn lệnh, chèn tham số `git`, path traversal. |
| Phân quyền | Frontend kiểm tra chủ app cho mọi thao tác; Hermes tra danh bạ từ frontend. Key AI riêng và chủ app được đăng ký từ phía server của frontend. |
| Phạm vi app (`app_scope.py`) | Mọi app chạy chung namespace nên mọi thao tác của AI/Hermes chỉ đụng tài nguyên mang đúng tên của app (`<app>`, `<app>-be`, `<app>-svc`…, so khớp chính xác). Manifest chỉ được chứa Deployment, StatefulSet, Service (ClusterIP), Ingress (tên miền của app), ConfigMap, Secret, PVC, Middleware; cấm hostPath, hostNetwork, privileged, đổi serviceAccount, tham chiếu Secret của app khác. Log, Ingress, sự kiện K3s gửi cho AI đều được lọc theo app. |
| Giới hạn tài nguyên | Ghi đè limits theo phân bổ trước mỗi lần apply; LimitRange mặc định cho namespace. |
| Cô lập mạng (`network_policy.py`) | `vibe-default-deny-ingress` chặn mọi kết nối vào Pod; `vibe-allow-ingress-controller` cho Traefik và health probe của kubelet; `vibe-app-<app>` cho các thành phần của cùng app gọi nhau. App A không kết nối được database hay service nội bộ của app B. Policy của app được tạo trước khi Pod chạy; lúc khởi động áp policy từng app trước rồi mới bật chặn mặc định. Chiều ra (Internet, DNS) không bị chặn. |
| AI CLI (`ai_sandbox.py`) | Antigravity và Claude Code chạy bằng user không đặc quyền `vibe-ai`, trong thư mục trống riêng mỗi lần gọi (xóa sau khi xong), môi trường đã bỏ mọi biến bí mật và `KUBECONFIG=/dev/null`. `vibe-ai` không đọc được `/opt/vibe-hosting` (mode 700: `.env`, mã nguồn các app, cấu hình) hay `/root` (kubeconfig, Docker Hub). Claude chạy thêm `--tools ""`; Antigravity dùng hộp cát terminal mặc định. Không có `vibe-ai` thì CLI chạy bằng root nhưng bị chặn hết công cụ. Prompt luôn nhắc model không truy cập máy chủ, không bịa số liệu. |
| Agent | Chỉ làm việc trên bản sao mã nguồn của đúng app, bằng `vibe-ai`; thay đổi phải qua kiểm tra và được người dùng duyệt. |
| Bí mật | GitHub token được che trong log; File Manager không cho truy cập `.git`; key AI mã hóa Fernet; token Telegram và file trạng thái mode 600. Hermes che giá trị biến môi trường bí mật khi hỏi duyệt. |
| Giải nén | Chống Zip Slip / path traversal với ZIP và TAR. |

Trước đây Antigravity chạy bằng root với `--dangerously-skip-permissions`: một câu chat ở AI Debug như "kiểm tra dung lượng VPS" làm agent chạy lệnh trên máy chủ backend. Lỗ hổng này đã được đóng bằng cách chạy CLI bằng `vibe-ai` như trên.

---

## Dọn dẹp tài nguyên

- **Máy build** (`docker_janitor.py`): sau mỗi pipeline chạy `docker builder prune --max-used-space 5gb` và `docker image prune -a --filter until=2h` (image đã có trên Docker Hub; ngưỡng 2 giờ tránh xóa image của pipeline khác đang build). Luồng nền: khi khởi động và mỗi 6 giờ dọn như trên; mỗi 5 phút đo ổ đĩa, vượt 80% thì dọn mạnh (giữ 1 GB cache, tối đa 1 lần/giờ). Log `docker-janitor`.
- **Node K3s khi xóa app** (`node_image_cleanup.py`): tạo trên mỗi node một Job ngắn dùng crictl của K3s để xóa image của đúng app (`<app>`, `<app>-be`, `<app>-fe`); image có tag của repository khác được giữ lại; thử lại tối đa ~3 phút chờ Pod dừng hẳn rồi tự hủy. Nên đặt ngưỡng GC của kubelet thấp hơn mặc định trong `/etc/rancher/k3s/config.yaml`:

  ```yaml
  kubelet-arg:
    - "image-gc-high-threshold=70"
    - "image-gc-low-threshold=50"
  ```

- **Docker Hub** (`dockerhub_cleanup.py`, `GET`/`POST /api/dockerhub/cleanup`): xem trước rồi mới xóa repo mồ côi và tag cũ. Không bao giờ xóa repo trong `DOCKERHUB_PROTECTED_REPOS`, tag đang được K3s tham chiếu (kể cả ReplicaSet cũ để rollback), `DOCKERHUB_KEEP_TAGS` tag mới nhất, repo/tag vừa cập nhật trong `DOCKERHUB_GRACE_HOURS` giờ. Không đọc được K3s thì không xóa gì.
- **Xóa app** (`DELETE /api/apps/{app}`): xóa tài nguyên K3s, database, NetworkPolicy, image local, repo Docker Hub, image trên node, workspace, cấu hình, phân bổ, chủ app và log.

---

## Tài liệu API

**Xác thực:** header `X-API-Key: <VIBE_API_KEY>` cho mọi request trừ `/api/webhooks/*`. Hai endpoint upload file nén nhận thêm `X-Upload-Token` và giới hạn dung lượng theo `MAX_UPLOAD_MB` (mặc định 500).

| Mã lỗi | Ý nghĩa |
|---|---|
| `400` | Dữ liệu đầu vào không hợp lệ |
| `401` | Thiếu hoặc sai API key / chữ ký webhook |
| `403` | Vượt gói (không có database), key AI không thuộc chủ app |
| `503` | Máy chủ chưa cấu hình `VIBE_API_KEY` hoặc bot Telegram |

**Ràng buộc `app_name`:** chữ thường, số và `-`; tối đa 40 ký tự; không bắt đầu hay kết thúc bằng `-`.

### Triển khai và quản lý ứng dụng

| Method | Endpoint | Mô tả |
|---|---|---|
| GET | `/api/ai-providers` | Danh sách AI engine và model |
| GET | `/api/open-source-templates` | Danh mục CMS / mã nguồn mở |
| POST | `/api/deploy` | Repo đơn (JSON) |
| POST | `/api/deploy-with-db` | Repo kèm database, có thể kèm file backup (form-data) |
| POST | `/api/deploy-fullstack` | Frontend + backend + database (form-data) |
| POST | `/api/deploy-monorepo` | Monorepo (JSON) |
| POST | `/api/deploy-open-source` | CMS / mã nguồn mở với database trống (JSON) |
| POST | `/api/deploy-archive`, `/api/deploy-archive-with-db` | File nén (form-data, `X-Upload-Token`) |
| POST | `/api/apps/{app}/owner` | Frontend đăng ký chủ app trước khi triển khai |
| POST | `/api/apps/{app}/resources` | Phân bổ tài nguyên của app (áp ngay cho workload đang chạy) |
| DELETE | `/api/apps/{app}` | Xóa toàn bộ tài nguyên của app |
| GET | `/api/logs/{app}` | Log build / triển khai |
| GET | `/api/pod-logs/{app}?component=be\|fe&lines=200` | Log runtime (kèm log lần crash trước) |
| GET, POST | `/api/apps/{app}/config` | Đọc cấu hình / bật tắt CI/CD |
| POST | `/api/apps/{app}/sync-repo` | Cập nhật link repo và chủ GitHub |
| POST | `/api/apps/{app}/redeploy` | Build lại thủ công (`branch`, `target_component`, `pull_git`) |
| GET | `/api/apps/{app}/history?component=` | Lịch sử phiên bản |
| POST | `/api/apps/{app}/rollback` | Quay về phiên bản cũ (`revision: 0` = bản liền trước) |
| GET, POST | `/api/apps/{app}/health-report`, `/api/apps/{app}/verify` | Báo cáo / chạy lại Hermes Verifier |
| POST | `/api/webhooks/github/{app}` | GitHub Webhook (push) |
| POST | `/api/usage` | `{apps, include_nodes}`: tài nguyên đang dùng |
| POST | `/api/domains/check` | `{domains}`: HTTPS, thời gian phản hồi, hạn chứng chỉ |
| GET, POST | `/api/dockerhub/cleanup` | Xem trước / dọn rác Docker Hub |

### AI Debug (`/api/ai-debug`) và key AI (`/api/ai-credentials`)

| Method | Endpoint | Mô tả |
|---|---|---|
| POST | `/api/ai-debug/analyze` | Chẩn đoán (`app_name`, `component`, `user_note`, `ai_provider`, `user_id`) |
| POST | `/api/ai-debug/{app}/execute` | Thực thi hành động đã duyệt |
| POST | `/api/ai-debug/chat` | Chat về app |
| POST | `/api/ai-debug/agent` | Bắt đầu phiên Agent (chạy nền) |
| GET | `/api/ai-debug/agent/{id}?app_name=` | Trạng thái, câu trả lời, diff |
| POST | `/api/ai-debug/agent/{id}/apply` | Áp dụng các file đã chọn (`paths`, `rebuild`) |
| GET | `/api/ai-credentials?user_id=` | Key của người dùng (chỉ 4 ký tự cuối) và các hãng hỗ trợ |
| PUT | `/api/ai-credentials` | Thêm/sửa key (gọi thử trước khi lưu) |
| POST | `/api/ai-credentials/{id}/test`, `/{id}/default` | Gọi thử / đặt mặc định |
| DELETE | `/api/ai-credentials/{id}?user_id=` | Xóa key |

### File Manager (`/api/project`) và Telegram (`/api/telegram`)

| Method | Endpoint | Mô tả |
|---|---|---|
| GET | `/api/project/{app}/files`, `/file?path=` | Cây thư mục, đọc file |
| PUT | `/api/project/{app}/file` | Ghi file |
| POST | `/api/project/{app}/github/push` | Commit và push lên GitHub của người dùng |
| GET, PUT, DELETE | `/api/telegram/config` | Trạng thái / lưu (kiểm tra bằng `getMe`) / gỡ token bot |
| POST | `/api/telegram/link-code` | Tạo mã liên kết cho `user_id` |
| GET, DELETE | `/api/telegram/status`, `/api/telegram/link` | Trạng thái / hủy liên kết |
| POST | `/api/telegram/daily-report/test` | Gửi thử báo cáo hằng ngày cho admin |

Ví dụ:

```bash
curl -X POST http://localhost:8080/api/deploy \
  -H "X-API-Key: $VIBE_API_KEY" -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/<user>/<repo>.git", "app_name": "my-app",
       "app_domain": "my-app.example.com", "ai_provider": "hydrogen"}'

curl -H "X-API-Key: $VIBE_API_KEY" http://localhost:8080/api/logs/my-app
```

---

## CI/CD cho ứng dụng người dùng

1. Người dùng bật Auto Deploy; frontend dùng GitHub token của họ đăng ký webhook `https://<api-domain>/api/webhooks/github/<app>` (sự kiện **push**) với secret `GITHUB_WEBHOOK_SECRET`.
2. Khi có push, Vibe Core xác minh chữ ký rồi gọi [cicd_core.py](cicd_core.py): nhận diện kiểu app, chỉ build lại phần vừa được push, đồng bộ nhánh `main`/`master`, build image tag theo thời điểm (`<user>/<app>:<timestamp>`), push và rolling update.
3. Nếu app đã chuyển sang repo trên tài khoản của người dùng, webhook từ repo của tác giả gốc bị bỏ qua.

---

## Cài đặt và vận hành

### Yêu cầu

- Máy chủ Linux có Python 3.11, `git`, `tree`, Docker (đã đăng nhập Docker Hub), Nixpacks; tùy chọn `unrar`, `p7zip-full`.
- Cụm K3s có Traefik và cert-manager; `kubectl` truy cập được cụm từ máy chủ backend.
- AI: Antigravity CLI (`agy`) và/hoặc Claude Code CLI (`claude`); Hermes Agent gateway cho Hermes Butler/Verifier.

### Cài đặt API

```bash
git clone https://github.com/tobi1008/vibe-core.git /opt/vibe-hosting
cd /opt/vibe-hosting
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

kubectl apply -f cluster-issuer.yaml            # sửa email trước khi apply
kubectl create secret docker-registry regcred \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<docker-user> --docker-password=<docker-token>
```

### Cài user `vibe-ai` cho AI CLI

```bash
useradd -m -s /bin/bash vibe-ai
install -d -o vibe-ai -g vibe-ai -m 700 /home/vibe-ai/jobs
chmod 700 /opt/vibe-hosting

# Cài agy vào ~/.local/bin của vibe-ai (để tự cập nhật được), rồi đăng nhập từng CLI một lần
sudo -u vibe-ai -i      # chạy: agy (đăng nhập Google), claude rồi /login
```

Root chạy `sudo -u vibe-ai` không cần sudoers riêng. Kiểm tra: `sudo -u vibe-ai cat /opt/vibe-hosting/.env` phải bị từ chối.

### Tự cập nhật công cụ AI

Timer systemd `vibe-ai-tools-update.timer` (03:30 mỗi ngày) chạy `/usr/local/sbin/vibe-ai-tools-update.sh`: `agy update`, `claude update` bằng user `vibe-ai`, và `hermes update --yes`; Hermes có bản mới thì khởi động lại `hermes-proxy` và kiểm tra cổng 8645. Log ở `/var/log/vibe-ai-tools-update.log`.

### Biến môi trường

Tạo file `.env` trong thư mục gốc (đã nằm trong `.gitignore`).

| Biến | Bắt buộc | Mô tả |
|---|---|---|
| `VIBE_API_KEY` | Có | Khóa API trong header `X-API-Key` (`openssl rand -hex 32`). |
| `GITHUB_WEBHOOK_SECRET` | Khuyến nghị | Secret xác minh webhook GitHub, trùng với frontend. |
| `AI_KEYS_SECRET` | Khuyến nghị | Khóa mã hóa API key AI của người dùng; bỏ trống thì dẫn xuất từ `VIBE_API_KEY`. |
| `MAX_UPLOAD_MB` | Không | Dung lượng tối đa một request upload. Mặc định `500`. |
| `DOCKER_PASSWORD` | Không | Token Docker Hub quyền Read/Write/Delete để xóa image; bỏ trống thì dùng `docker login` sẵn có. |
| `DOCKERHUB_PROTECTED_REPOS`, `DOCKERHUB_KEEP_TAGS`, `DOCKERHUB_GRACE_HOURS` | Không | Dọn rác Docker Hub: repo không bao giờ đụng tới, số tag giữ lại (mặc định `3`), giờ chờ (mặc định `1`). |
| `AI_CLI_USER` | Không | User chạy AI CLI. Mặc định `vibe-ai` nếu máy có; đặt `0` để chạy bằng root (bị chặn hết công cụ). |
| `AI_AGENT_USER`, `AI_AGENT_JOBS_DIR`, `AI_AGENT_TIMEOUT` | Không | Chế độ Agent: user (mặc định `vibe-ai`), thư mục làm việc (mặc định `/home/vibe-ai/jobs`), thời gian tối đa (mặc định `600` giây). |
| `AGY_BIN_PATH`, `CLAUDE_BIN_PATH` | Không | Đường dẫn CLI nếu không nằm trong `PATH`. |
| `HERMES_API_URL`, `INTERNAL_BEARER_TOKEN` | Không | Hermes gateway (mặc định `http://127.0.0.1:8645/v1/chat/completions`) và bearer token. |
| `NETWORK_POLICY` | Không | `0` để không tạo NetworkPolicy. |
| `NAMESPACE_LIMIT_RANGE`, `NAMESPACE_DEFAULT_CPU`, `NAMESPACE_DEFAULT_MEMORY` | Không | LimitRange mặc định (`0` để tắt; mặc định `1000m` / `1Gi`). |
| `DOCKER_JANITOR` | Không | `0` để tắt tự dọn Docker. |
| `DOCKER_BUILD_CACHE_MAX`, `DOCKER_BUILD_CACHE_MIN`, `DOCKER_IMAGE_MAX_AGE` | Không | Cache giữ lại (mặc định `5gb`, khi dọn mạnh `1gb`), tuổi image được xóa (mặc định `2h`). |
| `DOCKER_JANITOR_INTERVAL`, `DOCKER_JANITOR_DISK_PCT` | Không | Chu kỳ dọn (giờ, mặc định `6`), ngưỡng ổ đĩa dọn mạnh (mặc định `80`). |
| `NODE_IMAGE_CLEANUP` | Không | `0` để không xóa image trên node K3s khi xóa app. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` | Không | Token/tên bot; thường không cần vì admin nhập token trên web. |
| `FRONTEND_URL` | Không | Địa chỉ frontend để tra danh bạ. Mặc định `https://vibe.quyenlt.com`. |
| `HERMES_BUTLER` | Không | `0` để tắt bot và giám sát. |
| `MONITOR_INTERVAL`, `MONITOR_DISK_PCT`, `MONITOR_MEM_PCT`, `MONITOR_CPU_PCT`, `MONITOR_CERT_DAYS` | Không | Giám sát: chu kỳ (giây, mặc định `300`), ngưỡng % (`85`/`90`/`90`), ngày còn lại của chứng chỉ (`14`). |
| `DAILY_REPORT_HOUR` | Không | Giờ gửi báo cáo hằng ngày (GMT+7). Mặc định `8`. |

### Chạy

```bash
python main.py                                   # phát triển, tự reload, cổng 8080
uvicorn main:app --host 0.0.0.0 --port 8080      # production (systemd vibe-api.service)
```

### Dữ liệu lúc chạy (`/opt/vibe-hosting`, không đưa vào Git)

| Đường dẫn | Nội dung |
|---|---|
| `workspaces/` | Mã nguồn từng app (`<app>`, `<app>-be`, `<app>-fe`, `<app>-monorepo`) |
| `logs/` | Log build từng app, log hệ thống (`system`, `hermes-*`, `docker-janitor`…) |
| `backups/`, `uploads/` | File backup database, file nén được tải lên |
| `apps_config.json` | Cấu hình app (repo, tên miền, database, CI/CD, AI engine) |
| `app_resources.json`, `app_owners.json` | Phân bổ tài nguyên và chủ của từng app |
| `ai_credentials.json` | Key AI của người dùng (mã hóa, mode 600) |
| `ai_sessions/` | Kết quả các phiên Agent (giữ 24 giờ, mode 600) |
| `telegram_config.json`, `telegram_links.json`, `hermes_butler_status.json` | Token bot, liên kết Telegram, nhịp tim bot |
| `hermes_prefs.json`, `monitor_state.json` | Model Hermes của từng người dùng, trạng thái giám sát |

### Tự động triển khai API

Mỗi lần push lên `main`, GitHub Actions chạy ruff và bộ kiểm thử, rồi SSH tới máy chủ chạy [update.sh](update.sh) (pull, cài dependency, khởi động lại `vibe-api`). Cần các secret `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`.

### Tùy chỉnh cho môi trường khác

| Giá trị | Vị trí |
|---|---|
| Tài khoản Docker Hub (`DOCKER_USER`), namespace (`K8S_NAMESPACE`) | [builder_core.py](builder_core.py) |
| Thư mục gốc `/opt/vibe-hosting` | [builder_core.py](builder_core.py), [main.py](main.py) và các module lưu dữ liệu |
| Domain frontend được gọi API (CORS) | [main.py](main.py) |
| Email Let's Encrypt | [cluster-issuer.yaml](cluster-issuer.yaml) |

---

## Kiểm thử

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

Bộ kiểm thử (`tests/`) giả lập kubectl, Docker, Git, Telegram và các AI CLI, bao phủ các luồng triển khai, CI/CD, bảo mật đầu vào, phạm vi app, giới hạn gói, NetworkPolicy, hộp cát AI, chế độ Agent, key AI riêng, Hermes Butler/Monitor/Verifier, usage và dọn dẹp.
