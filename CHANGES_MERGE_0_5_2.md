# 合并记录：0.5.2（0.5.0 + secure 包的部署向导）

日期：2026-11-09

## 底与来源

- **底**：`AIMS-backend.zip` 的 0.5.0（三线已合：chain 闭环 + industrial core + 0.4.1 加固，
  2026-09-29 20:08 的构建）。`AIMS-backend-2.zip` 是同版早几小时的旧构建，未采用。
- **来源**：`aims-backend-secure.zip`（0.3.0 底子 + 重做的加固）。它的加固与 0.5.0 的 0.4.1
  是同一套要求、只是写法不同，**不整体合并**；只取它独有的部署向导与文档。

## 版本号

0.5.2。0.5.1 是已在 0.5.0 代码里的开发环境放宽（见 `CHANGES_SECURITY_HARDENING.md`
“0.5.1 开发环境放宽”，版本号字符串当时仍写 0.5.0）；本次合并不再沿用 0.5.1。

## 搬过来的（secure 包独有）

| 内容 | 来源 | 说明 |
|---|---|---|
| `app/cli/setup.py` | secure `app/cli/setup.py` | 一步初始化向导：密钥 + 证书 + 首个管理员，幂等 |
| `app/cli/manage_users.py` | secure `app/cli/manage_users.py` | 用户运维：create-admin / reset-password / activate / list（0.5.0 的 `app.cli` 没有 list/activate） |
| `app/cli/devcert.py` | secure `app/cli/devcert.py` | 生成本机自签名开发证书 |
| `app/core/secret_rules.py` | secure `app/core/secret_rules.py` | 密钥可接受性判断（向导与测试用） |
| `scripts/generate_secret.py`、`scripts/manage_users.py`、`scripts/generate_dev_cert.py` | secure `scripts/` | 向导的脚本入口 |
| `SECURITY.md` | secure `SECURITY.md` | 安全说明；**实现位置按 0.5.0 的模块结构逐行修正过**（见下） |
| `deploy/nginx.conf` | secure `deploy/nginx.conf` | 反向代理备选；compose 仍接 Caddy，未改 compose |
| `tests/test_setup_wizard.py` | secure `tests/test_setup_wizard.py` | 向导测试（11 项） |

## 为接线新写的 / 改的

- **新 `app/services/auth_service.py`（适配器）**：secure 的向导与用户管理 CLI 引用
  `app.services.auth_service` 的四个函数（`has_any_user`、`create_admin_user`、
  `invalidate_user_sessions`、`write_audit`）。本文件**不是** secure 那个合一的
  `auth_service.py`——0.5.0 的认证仍是拆开的实现（`app.core.security` /
  `app.services.auth_sessions` / `app.services.audit` / `app.db.init_db`），
  这里只是在它们之上包一层同名同签名的薄壳，让向导代码可以原样搬过来。
  `create_admin_user` 还会像 0.5.0 自己的 `create-admin` 一样补种权限（secure 版只种角色）。
- **`app/cli.py` → `app/cli/__main__.py`**：0.5.0 的单文件 CLI 改成包，内容不动；
  `python -m app.cli gen-secret/create-admin/reset-password/unlock` 照常可用，
  同时 `from app.cli import setup`、`app.cli.manage_users`、`app.cli.devcert` 可以导入。
  0.5.0 代码里没有任何 `import app.cli`，只有文档字符串提到命令名，故安全。
- **`app/db/init_db.py`**：加 `_ensure_roles = _seed_roles` 别名（secure 的
  `manage_users.py` 直接从 `init_db` 导入 `_ensure_roles`；两者是同一份角色种子，
  secure 版末尾多一次 commit，这里用 flush 即可，调用方随后会 commit）。
- **`app/cli/setup.py`、`app/cli/manage_users.py`**：各一处小改——0.5.0 的
  `validate_password_strength` 不合格时**抛 ValueError**，secure 版是**返回原因列表**；
  两处调用都改成 try/except，对外提示文案不变（“密码不合格：…”/“密码不符合策略：…”）。
- **`run_standalone.py`**：`main()` 开头加子命令分发：`setup`（向导）、
  `generate-secret`、`create-admin` / `reset-password` / `activate` / `list`
  （用户管理）；配置错误提示里加了向导入口。
- **`app/main.py`**：版本号 0.5.0 → 0.5.2（三处）。
- **`SECURITY.md` 的修正**（对照 0.5.0 代码逐项核对过）：
  demo JWT 的 claim 要求去掉 `tv`（0.5.0 不认 token_version）；
  登录限制改为用户名 5 次 / IP 20 次 / 15 分钟、实现位置 `app/api/v1/auth.py`；
  refresh 实现位置改为 `app/services/auth_sessions.py`；
  审计实现位置改为 `app/core/middleware.py` / `app/services/audit.py`；
  上传检查实现位置改为 `app/services/upload_security.py`；
  迁移说明改为 `009`（`refresh_tokens`、`login_failures` 表与审计索引）；
  日志提示改为实际的 `still used its default demo password`；
  HTTPS 推荐仍写 Caddy（compose 接的是 Caddy），nginx.conf 注明是手动部署备选。

## 刻意没搬的

- **secure 的 `app/services/auth_service.py`（合一实现）**：与 0.5.0 的拆分实现重复，
  且依赖 secure 的 `config.py` / `security.py` / `auth.py` / `init_db.py`；
  整搬会回退 0.4.1 与 industrial core。向导需要的四个函数由上面的适配器提供。
- **secure 的迁移 `007` / `008`**：0.5.0 的迁移链是线性的 `007 → 008 → 009` 且有测试锁定；
  secure 的 `007/008` 与之同名不同内容，不能并存。
- **secure 的 `app/core/config.py` / `security.py` / `app/api/v1/auth.py` /
  `app/api/v1/system.py` / `app/db/init_db.py` / `run_standalone.py` 全文**：
  都是 0.3.0 底子的旧版，搬过来会丢 0.5.0 的 industrial core 与 0.5.1 放宽。
- **secure 的 `app/core/upload_security.py`**：与 0.5.0 的
  `app/services/upload_security.py` 是同一套逻辑换了位置，路由用的是后者，不重复搬。
- **secure 的 `run_standalone.py` 的“缺配置时交互问要不要跑向导”**：会改 0.5.0 的
  启动失败路径，未搬；向导入口是 `run_standalone.py setup` 与配置错误提示。

## 行为差异（相对 secure 包，如实说明）

- **没有 `token_version`**：0.5.0 的 `users` 表没有该列，访问令牌是短时 JWT。
  改密码 / 重置密码 / `logout(all_sessions)` 会立即撤销**刷新令牌**，
  已签发的访问令牌按 15 分钟 TTL 自然过期，而不是立即失效。
  `app/services/auth_service.py` 的模块文档里写明了这一点。
- **密码策略是 0.5.0 的**：除长度/黑名单外，还要求至少 3 类字符（大小写/数字/符号）
  且不同字符不少于 6 个（secure 版不要求字符种类）。向导测试的弱口令用例在该策略下
  仍全部被拒，见测试结果。
- **nginx 未接进 compose**：`deploy/nginx.conf` 只是备选文件，`docker-compose.yml`
  仍用 Caddy；用 nginx 需手动改 compose 或单独部署。

## 验证

- `tests/test_setup_wizard.py`：13 项全部通过（密钥生成/替换/BOM 处理/幂等/首个管理员/
  弱口令拒绝/端到端非交互）。
- 全量 pytest：与未改动的 0.5.0 底子逐项对照失败清单（见测试输出），除新增的向导测试外
  无新增失败；`test_ai_cores_download` 的 1 项失败在底子上同样失败（沙箱环境问题，与合并无关）。
- `python -m app.cli gen-secret` 与 `python run_standalone.py setup --help`、
  `python scripts/manage_users.py --help` 均可跑；新模块导入全部通过。
- 测试里一处改动：`test_generated_env_is_accepted_by_the_real_settings` 原断言
  `REQUIRE_HTTPS is True`，0.5.0 的 `REQUIRE_HTTPS` 默认是 `None`（开发环境不强制，
  0.5.1 放宽），改断言为 `tls_direct`（向导生成的证书确实让服务直接以 HTTPS 提供），
  测试意图不变。
