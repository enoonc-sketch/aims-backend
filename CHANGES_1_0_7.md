# AIMS 1.0.7 变更说明（2026-11-13）

1.0.7 在 1.0.6 基础上改了两件事：默认口令强制改密从“前端拦”升级成“服务端也拦”，
角色页新增“重置用户密码”（超级管理员给别人改密码）。默认账号仍是 admin / admin
（全新安装），已有数据库的密码一律不动。

## 服务端强制改密

- 之前 1.0.6 的强制改密只在前端拦：后端返回标志，前端把用户送去改密码、藏起其他
  页面。懂接口的人直接调 API 还是能用。1.0.7 在服务端也拦：FastAPI 全局依赖
  `enforce_password_change` 检查每个 `/api/*` 请求，被标记的账号调登录、查自己、
  改密码、登出、刷新以外的任何接口都返回 403 `password_change_required`。
  未登录的请求不受影响，仍由原有鉴权返回 401。
- 标志有两个来源，取“或”：一是数据库 `users.must_change_password` 列，用默认口令
  建账号时打上，用户自己改密码后清零（别人替他重置密码不会清零——被别人给过密码
  的账号仍应自己改一次）；二是现算检查——开发管理员账号的口令哈希仍是内置默认
  口令。第二条覆盖老库升级：1.0.6 及更早的库没有这一列，管理员又一直没改过默认
  口令时，迁移只会补列（默认 False），没有这一条就漏拦了。
- 新增开关 `DEV_ADMIN_FORCE_PASSWORD_CHANGE`（默认 True）：关掉后两条来源都不触发，
  即不强制改密码。自定义口令（BOOTSTRAP_ADMIN_*）建的账号、生产环境永远不触发。
- 新增数据库迁移 `010_must_change_password`（接在 `009_security_hardening` 后面，
  幂等：列已存在就跳过）；独立 EXE 的旧 SQLite 库在启动建表时也会补这一列。
- 首次登录凭据文件（first-login.txt）里默认账号那一行改成：“首次登录时系统会要求
  先在「系统设置 → 修改密码」里改成自己的密码，改完才能使用其他功能。”
- 前端：`aimsFetch` 收到 403 `password_change_required` 时提示“请先修改密码”，并
  直接跳到强制改密码页（覆盖页面启动时自动取数先于 `/auth/me` 的竞态）；改密码页
  顶部新增黄色提示条（平时隐藏，被要求改密码时才显示），中英文词条 `pwForced` /
  `pwForcedShort`。
- `tools/e2e_smoke.py`：登录后若被要求改密码，先改成临时强口令再继续跑后面的检查。

## 角色页新增“重置用户密码”

- 系统设置 → 角色页新增“重置用户密码”区（用户下拉框 / 新密码 / 确认新密码 / 重置
  按钮），只有超级管理员可见；调后端已有的 `PATCH /system/users/{id}`（超级管理
  员专用），新密码按口令策略校验，重置后该用户在所有设备上退出登录、旧密码立即
  作废。自己的密码仍走“修改密码”标签页，下拉框里排除自己。
- 打开角色页签时加载用户列表（最多 200 个）；已停用的账号在下拉框里标注出来。
- 新增中英文词条 `rpTitle` / `rpHint` / `rpUser` / `rpNew` / `rpConfirm` /
  `rpSubmit` / `rpNoUsers` / `rpDisabled` / `rpErrPick` / `rpConfirmAsk` /
  `rpDone` / `rpErrForbidden`。

## 测试

- SQLite 全量：714 通过、2 跳过（`test_industrial_reads.py:29` 无网关、
  `test_module_imports.py:59` 缺 xlwt）、0 失败。新增测试 9 个：
  `tests/test_admin_reset_password.py`（3 个：超管能重置他人密码且旧密码旧会话
  立即失效、普通用户改不了别人的密码、弱口令被拒绝），
  `tests/test_password_force_change.py` 新增 6 个（被标记的账号调其他接口被 403
  拦、只放行 me / 改密码 / 登出 / 刷新；改密码后服务端拦截解除；开关关掉不强制；
  自定义安装口令不触发；普通已登录用户不受影响；旧 SQLite 用户表补列）。
- 未验证（需真机/真环境）：PostgreSQL 全量、真实 Windows 安装（含内置 PostgreSQL
  路径）、真实 llama-cpp-python 安装、真实设备通讯、真实厂商云端下载。
