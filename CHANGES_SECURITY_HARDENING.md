# 0.4.1 — 安全加固（强制项）

> **0.5.1 放宽说明**：开发/独立 EXE 环境下 SECRET_KEY 可留空、允许开发管理员，见文末“0.5.1 开发环境放宽”。生产环境的要求不变。

| # | 要求 | 实现 | 位置 |
|---|------|------|------|
| 1 | SECRET_KEY 必须外部注入 | 无默认值；空值/占位符/<32 字符 → 启动失败（所有环境） | `app/core/config.py` |
| 2 | 禁止默认 admin 密码 | 不再种子任何账号；首个管理员经 `python -m app.cli create-admin` 或一次性注入 `BOOTSTRAP_ADMIN_*`（须满足密码策略）；旧库中仍用演示口令的账号启动时自动停用 | `app/db/init_db.py`, `app/cli.py` |
| 3 | 禁止 demo JWT | 删除 `demo.` 令牌与 jose 缺失回退；仅接受已签名、校验 iss/aud/exp/nbf/jti、`type=access` 的 HS256 令牌 | `app/core/security.py` |
| 4 | 禁止 SHA256 密码回退 | 仅 bcrypt（缺库则导入失败）；历史 `sha256$` 哈希一律验证失败；移除 passlib | `app/core/security.py` |
| 5 | CORS 白名单 | 显式来源列表；`*`、`null` 一律拒绝；生产仅 https；空列表 = 不发 CORS 头；前端默认同源 | `config.py`, `main.py`, `frontend.html` |
| 6 | HTTPS | 生产必须直连 TLS（`SSL_CERTFILE/KEYFILE`）或可信反代（`TRUSTED_PROXY_IPS`）；明文请求 403；HSTS；compose 用 Caddy 终止 TLS，API/DB 不对外发布端口 | `middleware.py`, `run_standalone.py`, `docker-compose.yml`, `deploy/Caddyfile` |
| 7 | 登录失败限制 | 按用户名(默认 5 次)与 IP(默认 20 次)滑动窗口(15 分钟)；锁定期间不校验密码，返回 429 + Retry-After；未知用户同样计数、同等耗时 | `app/api/v1/auth.py` |
| 8 | Token refresh | 15 分钟 access + 7 天单次使用 refresh；轮换；旧令牌重放 → 整个会话族撤销；库中只存 HMAC 摘要；登出 / 改密 / 停用用户即撤销 | `auth.py`, `models/system.py`, migration 008 |
| 9 | 操作审计 | 中间件记录所有 POST/PUT/PATCH/DELETE（操作者、IP、状态码、结果、耗时）；认证事件、用户管理、设置变更、CLI 操作显式入库；不记录请求体与口令 | `middleware.py`, `services/audit.py` |
| 10 | 文件上传安全 | 所有 10 个上传端点统一走 `read_upload`：文件名清洗、扩展名白名单、分块限量读取、魔数/内容嗅探（csv/xlsx/xls）、xlsx 拒绝宏/嵌入对象/外部链接/加密/路径穿越/zip 炸弹、行列数上限、请求体大小前置限制、`defusedxml`；审计记录文件名+大小+SHA-256 | `services/upload_security.py`, `table_import.py` |

## 升级步骤
1. `python -m app.cli gen-secret` → 写入 `SECRET_KEY`（环境变量或未入库的 `.env`）。
2. 设置 `CORS_ORIGINS`（若 UI 由本服务 `/ui` 提供可留空）。
3. 生产：`APP_ENV=production` + TLS 方案（见 `.env.example`）；`POSTGRES_PASSWORD`、`DOMAIN` 必填。
4. 创建管理员：`python -m app.cli create-admin --username <name>`。
5. 数据库迁移：`alembic upgrade head`（008：refresh_tokens、login_failures、审计索引）；SQLite 独立模式由 `create_all` 自动建表。
6. **旧库**：`admin/system/operator/maintainer` 若仍是演示口令会在启动时被停用，用 `python -m app.cli reset-password --username <name>` 恢复。

## 破坏性变更
- `ACCESS_TOKEN_EXPIRE_MINUTES` 默认 1440 → 15，上限 60；客户端须使用 `/auth/refresh`（内置前端已适配）。
- 密码策略：≥12 字符、至少 3 类字符、非常见口令、不含用户名（创建/修改用户、改密、CLI）。
- 前端不再有“离线演示登录”。
- 生产环境 `/docs` 默认关闭（`ENABLE_DOCS=true` 可开）；`/health` 在生产只返回 status/version。
- 上传：拒绝含宏/外链/嵌入对象的 xlsx 与内容和扩展名不符的文件。

## 新接口
`POST /auth/refresh` · `POST /auth/logout` · `POST /auth/change-password` · `GET /system/audit-logs?action=&user_id=`

## 已知限制（未在本次范围内）
- 读接口(GET)仍保持未认证开放（沿用 0.4.0 的设计）；若数据敏感，应把 `get_current_user` 加到各路由。
- `crypto_util.py` 仍由 SECRET_KEY 派生加密密钥：轮换 SECRET_KEY 后需重新录入外部站点密码。
- 登录限流存于数据库，多实例部署下天然共享；如需更强的边缘限流仍应在反向代理层叠加。

## 0.5.1 开发环境放宽（生产不变）

| 项 | 生产（`APP_ENV=production`） | 开发 / 独立 EXE |
|---|---|---|
| SECRET_KEY | 必须外部注入，≥32 位，非占位符，否则拒绝启动 | 可留空：首次启动随机生成并保存到数据库同目录 `.dev_secret_key`（重启后令牌仍有效）；过短/占位符只警告 |
| 默认管理员 | 禁止；`ALLOW_DEV_ADMIN=true` 会拒绝启动 | 空库且未注入 `BOOTSTRAP_ADMIN_*` 时创建 `admin` / `admin123`（可用 `ALLOW_DEV_ADMIN=false` 关闭，或用 `DEV_ADMIN_*` 改名/改口令） |
| 旧库演示口令账号 | 启动时自动停用 | 只放行与 `DEV_ADMIN_*` 完全一致的账号；`system` / `operator` / `maintainer` 仍会被停用 |

其余项（无 demo JWT、无 SHA256 回退、CORS 白名单、登录限流、refresh 轮换、审计、上传检查、密码策略）在所有环境保持不变。开发管理员创建时**不**经过密码策略，但改密码、新建用户仍需满足策略。

### 前端（frontend.html）
- 登录：Enter 提交、显示/隐藏密码、Caps Lock 提示；401 / 403（停用或需 HTTPS）/ 422 / 超时 / 不可达各有明确提示；429 时按 `Retry-After` 倒计时并禁用登录按钮。
- 会话：按 access 令牌 `exp` 提前约 1 分钟自动 refresh；401 时刷新一次并重放请求；refresh 失败（令牌失效/被撤销）→ 清除令牌并弹出全局提示，一键跳到 系统→登录；多标签页通过 `storage` 事件同步登录/登出；已登录时显示当前用户与“Sign out”。网络抖动或 429/5xx 不会误清令牌。
- 上传：所有表格导入在发送前检查扩展名（csv/xlsx/xls）、空文件、≤20 MB、文件名、魔数与扩展名是否一致（伪装的 xlsx/xls、二进制 csv）、xlsm 等宏文件给出明确提示；服务端 413/401/429 转成易懂文案；文件名与服务器文本渲染前做 HTML 转义。前端检查只是提前反馈，服务端校验仍是最终依据。
