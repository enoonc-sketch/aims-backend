# AIMS 1.0.0 变更记录

**发布日期**：2026-11-11（源码版本；Windows 安装包需在 Windows 构建机执行 `build\build_x64.ps1`）
**上一版本**：0.6.0（桌面化基线）
**回归状态**：pytest **515 passed / 2 skipped**；部署 selftest **34/34**；前端无头浏览器冒烟通过

---

## 一、安全整改（阶段 1）

| 项 | 内容 |
|---|---|
| H-1 | 21 个业务路由统一挂认证依赖；外部数据源与 pip 安装等管理端点收敛为超管；实测匿名请求全部 401 |
| H-2 | 开发默认管理员（admin/admin123）默认关闭，须显式 `ALLOW_DEV_ADMIN=true`；生产环境强制拒绝 |
| M-3/M-4 | 新增 `ssrf_guard`：外部抓取源强制公网 http/https；通讯测试采用「已登记资产放行、未登记内网拒绝」策略 |
| M-11 | `python-jose` → **PyJWT**；`python-multipart>=0.0.18`；`fastapi>=0.115.6`（CVE 修复） |
| M-8 | Fernet 密钥改为 HKDF 派生，兼容旧密文回退 |
| M-10 | 交付版（frozen）默认关闭 `/docs`；模板包上限统一 64MB |
| L 项 | 前端网关表格 XSS 转义、CSP 响应头、登录失败记录过期清理 |
| **新增** | 报警确认人一律取登录身份，忽略客户端自报 `user_id`（防止伪造审计） |

## 二、数据与稳定性（阶段 1）

- 新增 `retention` 服务：measurements / acquisition_events / audit_logs / dashboard_snapshots /
  external_sync_logs / login_failures 按配置天数分块清理，每日 03:30 执行并在启动时补跑；
- 导入改分块 bulk 写入，保留单事务回滚语义；启动回收孤儿 `processing` 批次；
- 通讯中断巡检接入调度器（此前无任何调度者，独立部署时永远不会自动发现断线）；
- 修复：模板导入 NameError、网关删除 500→409、TAG 并发 upsert、刷新令牌条件 UPDATE、连接池参数化。

## 三、打包与运行环境（阶段 2）

- **Critical 修复**：`build_exe.spec` 的 `excludes` 误排 `psycopg2`/`pgvector` ——
  PyInstaller 中 excludes 优先于 hiddenimports，原配置打出的 EXE 无法连接 PostgreSQL；
- `requirements-exe.txt`/lock 补入 `psycopg2-binary`、`pgvector`；
- 移除无引用的 `passlib`；新增 `pypdf`（PDF 解析）。

## 四、AI 与后台组件管理（阶段 3–5）

- **模型生命周期分离**：`POST /ai/cores/{id}/unload`（释放内存保留文件）与
  `DELETE /ai/cores/{id}?confirm=true`（超管，禁删激活模型/内置规则包，路径受控，删后可在 manifest 重新下载）；
- `GET /system/components`：组件清单（版本/状态/许可证/路径）+ 目录占用；
- `GET /ai/engine`：引擎版本/路径/架构/推理参数/硬件探测（不支持项如实 null）；
- `POST /ai/cores/scan-local`：扫描模型目录未登记 GGUF（魔数校验 + SHA-256 + 来源 local-import，幂等）；
- 组件依赖表 `app/deploy/dependencies.py`，`deploy stop` 输出受影响组件；
- Launcher 启动时对 embedding 模型做真实预热，模型损坏在启动报告即暴露。

## 五、业务整合（阶段 7）

- **报警中心页面**（新增）：状态/等级筛选、确认、解决、阈值规则列表，全部走真实 API；
- **系统管理增强**：用户管理（启用/停用）、审计日志、后台组件三个新页签；
- **故障 PDF 分析真实化**：pypdf 提取文本 → 位号/报警码/测量值/故障关键词 → 规则包候选诊断；
  扫描件明确提示需要 OCR；含 `/JavaScript`、`/Launch` 的 PDF 拒收；结果不写库、不建单；
- **假数据清理**：工单 KPI 不再随机（无后端时显示「—」）；刷新按钮改为重载真实数据；
  AI 演示路径不再编造 RUL/健康分；未连后端时看板显示「演示数据」横幅；
- **industrial 影子域补齐**：connections/tag-mappings/acquisition-events/quality-events/models/ai-feedback
  六个只读端点（此前 6 张表只写不读）；
- **全局限流（M-7）**：内存令牌桶，API 600 次/分/IP、认证端点 20 次/分/IP，429 + Retry-After。

## 六、安装与升级

- 版本号单一来源推进到 **1.0.0**（`app/deploy/__init__.py` 与 `installer/AIMS.iss`）；
- 安装器新增**降级防护**：已安装版本更高时警告并允许中止；
- 卸载默认保留用户数据（原有行为，未变）。

## 七、未完成（需真实 Windows）

- `ISCC` 编译安装包、干净 Win11 x64/ARM64 安装/升级/卸载实测、代码签名；
- TimescaleDB 无 Windows 可再分发二进制（BRIN 索引兜底，文档已如实说明）。

---

## 八、1.0.0 增量修复（2026-11-11，模板中心认证 + Windows 无 CMD）

### 模板中心「加载失败：未认证」

真实登录态下列表本可正常加载，问题出在**失效态的提示缺失**与两处确定性缺陷：

- `aimsFetch` 对"没有 refresh Token 的 401"从不触发全局会话失效提示 → 页面只显示英文
  `Not authenticated`，用户不知道要去重新登录。现在：受保护接口 401 且**发请求时带过
  Token** → 统一弹出「登录已失效，请重新登录（System → Login）」；匿名访问保持静默，
  避免首次打开就误报"已失效"；
- 模板页 401 渲染中文引导框（「去登录」/「重试」），不再是一行英文报错；
- 修复 `tplLoadCategories` catch 分支引用未定义 `box` 导致的 `ReferenceError`；
- 模板导出（blob 下载）同步接入失效提示；
- 未登录时前端不再向 `/devices`、`/dashboard/stats`、`/gateways` 发受保护请求
  （首屏零 401；登录成功后自动 `refreshData()` 换成数据库真实数据）。

### 模板权限（原来只校验"已登录"）

- 新增 `_ensure_can_modify`：编辑 / 删除 / 版本回退仅**创建者本人或管理员**；
- 系统内置模板：改名、改分类、删除对**所有人** 403（管理员只可改描述等次要字段）；
- 读取、导出、复制、收藏、设为默认、应用、导入对登录用户保持不变（不破坏既有功能）。

### Windows 安装版：全程无 CMD 窗口

- `build_exe.spec` → `console=False`（Windows GUI 子系统）；清理 PyJWT 迁移遗留的
  `jose` 隐藏导入；
- `run_standalone.py`：移除 `os.system("pause")`（会弹 cmd），改为无控制台时用原生
  MessageBox；新增 `AIMS_DEBUG_CONSOLE=1` 调试控制台（AttachConsole / AllocConsole）；
- 补齐 `CREATE_NO_WINDOW`：taskkill、powershell 建快捷方式、Edge `--app` 启动、
  schtasks 清理、pip 安装；
- 安装器：新增「AIMS 调试模式（控制台）」开始菜单入口；「备份与恢复」不再直连 `.ps1`
  （会弹 PowerShell 窗口），改为 `-WindowStyle Hidden`；
- **后端崩溃看门狗**（新）：此前只有 `--headless` 排障模式监控后端退出，正式用户走的
  WebView2/托盘路径后端崩溃只会白屏。现在所有模式统一：日志留痕 → 有限次自动重启
  （1.5s/3s/6s 退避）→ 连续 3 次仍失败则弹窗告知并停止重试（防崩溃-重启死循环）。

### 测试

- 新增 `tests/test_template_auth.py`（8 例）、`tests/test_no_console_window.py`（9 例，
  AST 静态审计所有子进程调用是否隐藏窗口 + 打包/安装器断言）、
  `tests/test_launcher_watchdog.py`（6 例，重启策略与进程清理）；
- 全量：**538 passed / 2 skipped**（原 523 + 15）。

---

## 九、阶段 8/9（安装升级恢复 + 正式测试发布）

### 安装链修复（会在真实装机时暴露的问题）

- `installer/AIMS.iss`：「AIMS 备份与恢复」快捷方式指向并不存在的
  `tools\Backup-Aims.ps1`（真实文件是 `backup.ps1`）→ 装完点击必然报错。已改正，
  并改由 `tools\run-hidden.vbs` 以窗口样式 0 启动（`-WindowStyle Hidden` 仍会闪一下
  控制台），完成后用原生弹窗告知成功/失败与备份路径（`backup.ps1 -Notify`）。
- **全部 18 个 `.ps1` 与 `AIMS.iss` 补 UTF-8 BOM**：Windows PowerShell 5.1 与 ISCC
  按系统 ANSI 代码页读取无 BOM 文件，安装向导文案、错误提示、备份日志会整体乱码
  ——这是"装完才看得见"的缺陷，现在由测试锁死。
- `build/build_common.ps1`：`Assert-FileHash` 在无基线时静默自动记录，等于
  "第一次下载什么就信任什么"。改为无基线必须显式 `-UpdateLock`，错误信息引导把
  `build/vendor.lock.json` 入库。
- `build/package_release.ps1`：复制 tools 时用 `-Include`（在 `-LiteralPath` +
  无 `-Recurse` 下不生效）→ 改为扩展名过滤；新增安装说明/管理员手册/安全审计报告，
  发布说明、SBOM、性能基线按通配收集。

### 阶段 9 测试发现并修复的缺陷

| # | 缺陷 | 修复 |
|---|---|---|
| 1 | 安装根目录不存在时 `subprocess` 的 cwd 缺失被误报成"找不到 PostgreSQL 程序" | `pg.run` 先建工作目录，并区分"程序缺失"与"目录缺失"两种 FileNotFoundError |
| 2 | `pg_ctl` 在但数据目录缺失时静默回落 SQLite（用户以为正常，数据在别处） | `runtime.bootstrap` 直接报错，给出数据目录/环境变量排查路径 |
| 3 | 普通用户 `GET /system/users` 返回 200，可枚举全部用户与超管标志 | 依赖改 `get_current_superuser` |
| 4 | 普通用户 `POST /ai/cores/scan-local` 可写模型注册表 | 同上 |
| 5 | 重复导入 CSV 触发 `UniqueViolation` → HTTP 500 + 堆栈 | 幂等 `INSERT … ON CONFLICT DO UPDATE`（PG/SQLite 双分支）+ 接口层 409 兜底 |
| 6 | pypdf 3.17.4 有 103 项已知漏洞（PDF 是不可信输入） | 升级 7.0.1，三处依赖同步；复扫 50 依赖 0 漏洞 |

顺带修掉导入的性能问题：`df[c].iloc[i]` 逐格取标量把 2 万行 CSV 压到 3.7k 行/秒，
改 numpy 数组索引后 **13251 条读数/秒**（高于阶段 0 基线）。

### 新增工具（可复现，不是一次性脚本）

- `tools/security_audit.py`：静态路由认证覆盖 + 动态越权/令牌/响应头/节流审计，
  输出 JSON，有高危即非 0 退出（可卡发布流水线）
- `tools/perf_bench.py`：延迟分布、并发吞吐、导入吞吐、soak 稳定性（采样 RSS/线程）
- `docs/sbom-1.0.0.cdx.json`：CycloneDX 1.4，50 组件

### 新增文档

`docs/安装说明.md`、`docs/管理员手册.md`、`docs/安全审计报告.md`、
`docs/RELEASE_NOTES_1.0.0.md`，`docs/测试报告.md` 增第 10 节（阶段 8/9 验收记录）。

### 测试

- 新增 `tests/test_release_chain.py`（15）、`tests/test_rbac_admin_endpoints.py`（4）、
  `tests/test_measurement_import_idempotent.py`（2）
- 全量：**559 passed / 2 skipped**
- 实跑：`deploy provision` 8.5 s / 57 表；`doctor` 正常；`selftest` **34/34**；
  备份→删 6 万行→恢复 **390952 条完全一致**；90 s soak **0 错误、RSS 持平**


---

## 合版说明（2026-11-11，对照已发布 0.6.0 核实）

1.0.0 底包基于 0.6.0 之前的桌面版源码，合版时从已发布 0.6.0 恢复了：NO_PROXY 修复及回归测试（`ai_cores._http_client` 的 trust_env=False 回退，1.0.0 新增的模型生命周期代码原样保留）、CHANGES_0_5_4/0_5_5 合并版、丢失的 CHANGES_0_5_10.md 与 CHANGES_0_6_0.md、加固版 build-exe 工作流（ISCC 路径回退 + exit code 检查，示例标签改到 v1.0.0）、修好的 AIMS-Backend.iss（GUID 花括号 bug + 已发布 AppId，版本号改到 1.0.0）、bat 的 webview hidden-import、lock 的 pefile/pywin32-ctypes；LICENSES.txt 版本改到 1.0.0。1.0.0 自己的改动全部保留：spec 的 psycopg2/pgvector 排除修复与 console=False、AIMS.iss 的降级防护与备份快捷方式修复、全部 .ps1 与 AIMS.iss 的 UTF-8 BOM、PyJWT 迁移、安全整改与模板中心认证。工作流文件（build-exe.yml、build-installer.yml）不能用 push-files 推，须在网页上改。
