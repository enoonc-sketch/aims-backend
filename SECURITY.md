# 安全说明

> 本文件随 0.5.2 合并自 secure 包；实现位置已按 0.5.0 的模块结构修正（0.5.0 没有 `token_version` 字段、没有合一的 `auth_service.py`，见 `CHANGES_MERGE_0_5_2.md`）。

## 强制项与实现位置

| 要求 | 行为 | 实现 |
|---|---|---|
| `SECRET_KEY` 外部注入 | 无默认值；缺失、占位符、弱值、少于 32 字符 → **拒绝启动**。可用环境变量 / `.env` / `SECRET_KEY_FILE` | `app/core/config.py` |
| 禁止默认 admin 密码 | 种子代码不创建任何用户。旧库里仍能用公开默认口令登录的账号在**启动时被停用**并撤销其会话 | `app/db/init_db.py` |
| 禁止 demo JWT | 只接受 HS256 + 签名 + `exp/sub/iss/aud/jti/type=access`；`demo.*`、`alg=none`、缺 claim 一律 401；前端的离线演示登录已删除 | `app/core/security.py` |
| 禁止 SHA256 回退 | 只认 bcrypt；`sha256$…` 遗留哈希**永不通过**（账号需重置密码）；超过 72 字节的密码拒绝而非静默截断 | `app/core/security.py` |
| CORS 白名单 | 精确 origin；拒绝 `*`、`null`、带路径；默认空（不放行任何跨域）；不开 credentials | `config.py` / `middleware.py` |
| HTTPS | `REQUIRE_HTTPS=true`（默认）时明文请求 400；HSTS；`APP_ENV=production` 不允许关闭 | `app/core/middleware.py` |
| 登录失败限制 | 滑动窗口，存数据库：用户名 5 次 / IP 20 次，15 分钟；锁定期间正确密码也拒绝；未知用户名同样计数；不信任 `X-Forwarded-For` | `app/api/v1/auth.py` |
| Token refresh | 访问令牌 15 分钟；刷新令牌 7 天、只存 HMAC、**轮换**；旧令牌重放 → 整个会话族作废 | `app/services/auth_sessions.py` / `app/api/v1/auth.py` |
| 操作审计 | 所有写操作 + 401/403/429 + 认证事件 + 对审计/用户清单的读取；记用户、路由模板、实体 id、状态码、IP、请求 id；**不记请求体和查询参数的值** | `app/core/middleware.py` / `app/services/audit.py` |
| 文件上传检查 | 文件名净化、扩展名白名单、流式限长、魔数校验、xlsx zip 炸弹/宏/加密/路径穿越检查、行列数上限；9 个导入端点共用 | `app/services/upload_security.py` |

改密码、管理员改密码/停用、`logout(all_sessions)` 会立即撤销该用户**全部刷新令牌**；已签发的访问令牌是短时 JWT，按 15 分钟 TTL 自然过期（本版 `users` 表没有 `token_version` 字段，不做访问令牌立即失效）。

## 首次部署

**单机 / EXE（最省事）**：`AIMS-Backend.exe setup`（源码：`python run_standalone.py setup`）。一步生成 `SECRET_KEY` 写入 `.env`、生成本机 HTTPS 证书、创建首个管理员；重复运行是幂等的（不会换掉已有密钥、不会重复建用户）。密钥由这个显式的部署动作一次性生成并落在 `.env`（代码和镜像之外），应用本身仍然不会隐式生成或保存密钥；`.env` 里有密钥，请保护好目录权限（Windows 上不会被自动收紧）。

**手动 / Docker**：

```bash
python scripts/generate_secret.py                # 把输出写进 .env 的 SECRET_KEY（或 secret 存储）
python scripts/manage_users.py create-admin ops  # 交互输入密码，至少 12 位、不在常见口令黑名单
```
也可用 `ADMIN_BOOTSTRAP_USERNAME/PASSWORD`（仅库里没有任何用户时生效，创建后请删掉密码）。

## 升级已有部署（重要）

1. **先备份**，再跑迁移 `alembic upgrade head`（`009` 增加 `refresh_tokens`、`login_failures` 表与审计索引；SQLite 单机版由建表逻辑自动补齐）。
2. 设置真实的 `SECRET_KEY`。**更换密钥的代价**：所有旧会话失效；**已保存的外部数据源站点密码无法解密**（它们用 `SECRET_KEY` 派生的密钥加密），需在外部数据页重新填写。
3. 启动后看日志里 `still used its default demo password` 的行：哪些默认口令账号被停用、哪些账号还是 SHA256 遗留哈希（无法登录）。用 `manage_users.py reset-password <用户名>` 逐个恢复。
4. 前端已改为同源访问 `/api/v1`；如果你曾在设置页保存过 `http://…` 的 API 地址，要改成 `https://…` 或清空。

## HTTPS 部署方式

- **推荐**：反向代理终止 TLS（`docker-compose.yml` + `deploy/Caddyfile`，证书放 `deploy/certs/`；`deploy/nginx.conf` 是同等作用的手动部署备选，未接进 compose）。后端用 `--proxy-headers --forwarded-allow-ips=<代理地址>` 才会采信 `X-Forwarded-Proto/-For`；`*` 只适用于"后端端口只对代理可达"（compose 里 `expose` 而非 `ports`）。
- **直连**：`SSL_CERTFILE` / `SSL_KEYFILE`。本机测试：`python scripts/generate_dev_cert.py`（自签名，仅限开发）。
- `/health` 免 HTTPS 检查（容器健康检查走内网明文），且只返回 `{"status":"ok"}`。

## 已知限制（诚实清单）

- 前端把令牌放在 `localStorage`，页面若有 XSS 就能被读走。更强的方案是 httpOnly cookie，但那会引入 CSRF 面，需要另做设计；这里已经缩短访问令牌寿命、刷新令牌轮换并带复用检测来降低影响。
- 审计写入失败**不会**让业务请求失败（可用性优先），只记错误日志；需要"审计不落库就拒绝操作"的场景要改中间件策略。
- 审计表没有自动归档/清理，也没有防篡改（无哈希链）；需要合规级别时应把日志转发到只追加的外部存储。
- 审计中间件只记录写操作与被拒绝的请求；普通读取（含敏感数据读取）不记录。
- 上传检查不做病毒扫描；xlsx 内容由 openpyxl（defusedxml）解析，旧版 `.xls` 由 xlrd 解析，仅做魔数校验。
- 登录限制按"直连对端 IP"计数；多个用户共用一个出口 NAT 时，IP 维度上限（30 次/15 分钟）可能误伤。
- docker-compose / nginx 配置在这里没有实际启动验证（没有 Docker 环境），只做了配置审查。
