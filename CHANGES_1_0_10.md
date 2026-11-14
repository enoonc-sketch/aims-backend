# AIMS 1.0.10 变更说明

以 1.0.9 为底。两件事：① 恢复平行包核验（1.0.8 对已发布 1.0.4）指出的 7 处文字丢失/写错；
② 设置页补齐账号中心缺的功能。

## ① 核验恢复（只动文字，没有一行已发布代码修复丢失）

核验结论：1.0.8 没丢任何已发布代码修复，可以做 1.0.9 的底；但下面 7 处文字必须先恢复。
核验在原始 zip 的全新解压上跑 SQLite 全量：752 通过、2 跳过、0 失败，与 1.0.8 changelog 一致。

1. `README.md`：恢复已发布 HEAD 整节 `## 管理员密码设置`（约 57 行：setup 向导、
   list/create-admin/reset-password、EXE 调试控制台、密码策略），从已发布提交 e675294 原样恢复。
2. `docs/安装说明.md`、`docs/用户手册.md`、`docs/部署说明.md`：初始口令写错成 `admin`，
   改为 `admin123`（代码默认一直是 admin/admin123，用户确认：首次登录密码 admin admin123）。
3. `.env.example`：`(default admin / admin)` 改为 `(default admin / admin123)`，旧库重置提示同步改。
4. `tests/test_first_login_credentials.py`、`tests/test_password_force_change.py`、
   `tests/test_installed_default_admin.py`：docstring 里的 admin/admin 改为 admin/admin123
   （测试本体一直用 `DEFAULT_PW = "admin123"`，只改文字；另顺手改了 test_password_force_change.py:149
   一处同款过期注释）。
5. `app/deploy/cli.py`：`--admin-password` 帮助“默认 admin”改为“默认 admin123”。
6. `tools/e2e_smoke.py`：用法示例 `AIMS_ADMIN_PASS=admin` 改为 admin123，两处注释同步改。
7. `app/deploy/provision.py`：用 `--admin-password` 自定义口令时，控制台仍打印“默认账号 admin /
   admin123”，改为通用措辞（凭据见 first-login.txt），不再声称是默认值。

其余 admin/admin 字样是刻意保留的旧版遗留说明（老库仍能用旧口令登录、旧演示账号停用逻辑），不动。

## ② 设置页账号中心（新增）

此前设置页已有登录、改密码、建账号、权限勾选、用户停用、审计日志；这次补的是缺口：

- **删账号**：新增 `DELETE /system/users/{id}`（超管专用）。不能删自己，不能删最后一个
  超级管理员；删除即吊销该账号全部会话、记审计 `USER_DELETE`。用户页与设置页的账号列表
  都加了删除按钮（自己的那行不显示，后端同样会拦）。
- **新建 / 删除角色**：新增 `POST /system/roles`（code 只允许小写字母、数字、下划线，
  重复 409，不合规 422；新建角色初始没有任何权限）与 `DELETE /system/roles/{id}`。
  内置 admin 角色不可删；还有账号在用的角色不可删（先把账号移走），记审计。
- **角色页权限表改成实时数据**：以前角色页那张权限表是写死的（Admin/Operator/Maintainer/
  System 四列，与真实的 admin/engineer/viewer 三个角色根本对不上）。现在行是权限、
  列是角色，✓/– 直接取自 `GET /system/role-permissions`，并可建/删角色。
- **首次登录强制改密补齐**：管理员代建的账号（`POST /system/users`）与管理员替他人重置
  密码（`PATCH /system/users/{id}`）之后，该账号下次登录必须先改成自己的密码，否则服务端
  拦住其他接口（只放行改密码/登出/刷新）。自己改自己的密码不触发；本人走
  `/auth/change-password` 改完后标志清零。
- **我的资料**：新增 `PATCH /auth/me`，本人改自己的姓名与邮箱（不经过管理员）。超管标志、
  启用状态、密码都改不了（密码走改密码，状态与角色走账号管理）。设置页“当前登录账号”
  下方加了资料表单，所有登录用户可用。

## 测试

- 新增 `tests/test_system_account_admin.py`（12 项）：删账号（删后登录 401、旧刷新令牌 401、
  删自己 400、普通用户 403、不存在 404）、建/删角色（201/409/422、admin 不可删、在用不可删、
  移走后可删、普通用户 403）、代建账号与他人重置后首次登录 `must_change_password=true` 且
  其他接口 403、改密后标志清零、本人改资料与防提权（多传 `is_superuser`/`password` 字段无效）。
- 前端 `frontend.html` JS 语法检查通过；`admin123` 在前端只出现在“admin / admin123”这一处
  （现有测试 `test_access_permissions` 的断言仍成立）。

## 未验证（与 1.0.9 相同，仍需真机 / 真实环境）

- 真实 Windows EXE 上下载安装三个引擎、真实浏览器点锁定屏与新账号中心、PostgreSQL 全量。
- 双安装器互不识别仍是已知待定项（`AIMS.iss` 与 `AIMS-Backend.iss` 安装路径与 AppId 不同）。
