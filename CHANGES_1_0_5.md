# AIMS 1.0.5 变更说明（2026-11-13）

1.0.5 是 1.0.4 的合版：把 Claude 对话里对 1.0.0 源码做的中英文修复与
默认账号工作（三方合并：祖先 1.0.0 → 仓库 1.0.4 + 快照
AIMS-1_0_0-Source-i18n-fix）合进 1.0.4。1.0.1–1.0.4 的改动优先，
快照里与之重复的旧内容已丢弃；版本号统一升到 1.0.5。

## 从快照合入的内容

- **中英文翻译修复**：侧栏错位根因修复（改由各按钮自带的 `data-i18n`
  驱动，不再按位置映射）；云数据页约 300 条中英对照、动态提示（按钮、
  状态、日志、覆盖率、断点）、会话弹窗、上传前检查、导入结果接入翻译；
  模板中心、批量导入、数据分析页 200 多条动态提示接入对照表；`translateText`
  误拆分“完成:”等带冒号字符串修复；提示语里 `$&` 被错误替换修复。
  新增 `tests/test_frontend_i18n.py`（165 个断言）。
- **默认账号 admin / admin + 登录门禁**：不注入任何账号配置、空库启动
  自动创建 admin / admin（开发/桌面环境；生产环境仍然一律拒绝）；
  未登录进不了 AI 中心、知识库、模板中心、维护管理、资产管理、实验室、
  云数据、系统设置（侧栏显示 🔒，点击跳去登录，登完自动回原页面）；
  登录表单预填 admin 并提示默认账号。新增 `tests/test_frontend_gating.py`。
- **AI 中心标签修正**：两个“模型下载”实为“LLM 对话”被错标（与侧栏错位
  同源：按位置映射，加了“选型”后整体错位），现改为按 key 驱动：
  诊断 | 预测 | 健康评分 | 选型 | 模型应用 | 模型下载 / 推理引擎。
  “模型应用”页新增“去下载模型 / 安装引擎”入口；“模型下载 / 推理引擎”
  页顶部新增推理引擎状态条（已安装 / 未安装）。
- **顺手修的两个真 bug**：“安装推理引擎”按钮必定 401（接口要求超级
  管理员，前端却用不带令牌的请求，改走 `aimsFetch`）；“退出登录”报错
  （`doLogout` 里局部变量 `t` 遮住了翻译函数 `t()`，点退出抛错、本地
  令牌清不掉）。
- **安装版默认 admin/admin**：安装包 provision 也默认创建 admin/admin
  （实施人员可用 `--admin-password` 自定义，旧安装不受影响）；修了
  first-login.txt 从未写出的 bug（provision 读到的“是否新建管理员”
  标志永远是 False，现改为 init_db 跑完后读模块属性）。
  新增 `tests/test_installed_default_admin.py`。
- **登录锁定按 IP + 用户名分别计数**：输错 5 次后连正确密码也被锁住、
  别人可故意锁住管理员的问题，改为按来源 IP + 用户名组合计数；单 IP
  累计 20 次失败的上限仍保留（防用户名喷洒）。
  新增 `tests/test_login_lockout_per_ip.py`。
- **前端“修改密码”页**：系统设置新增“修改密码”标签页（中英文），调
  已有的 `/auth/change-password`，成功后保存新令牌。
- **模板分类英文显示层翻译**：20 个模板分类名在英文界面下显示英文
  （如“设备台账”→ Equipment Ledger），覆盖分类栏、模板卡片、详情页、
  导入预览、新建/编辑下拉；传给后端的值仍是中文原值；切语言时模板页
  自动重绘、已选分类保持。新增回归测试
  `test_template_category_names_all_have_english_labels`：以后后端
  新增分类、前端没补英文名时测试会报错。
- **品牌 LOGO 更新**：`assets/` 三件（logo、512 图标、ico）换成新版
  AIMS 标志（A + 轨道 + 柱状图 + AiMS 字样，浅色底）。
- **文档措辞修正**：部署说明、用户手册、安装说明、`installer/AIMS.iss`
  里 4 处“随机生成密码”旧说法改为默认 admin/admin；首次登录后改密
  入口统一写成「系统设置 → 修改密码」。
- **构建链**：新增 `.github/workflows/build-installer.yml`（云端 Windows
  自动构建安装包：依赖检查 → 内置 PostgreSQL/pgvector → PyInstaller
  打包 → 冒烟测试 → Inno Setup 编译 → 构建报告）。

## 合并时丢弃的快照旧内容（仓库 1.0.1–1.0.4 的改动优先）

- **快照悄悄丢掉的 0.5.5 NO_PROXY 回退（已恢复）**：快照的
  `app/services/ai_cores.py` 把 `_http_client()` 里 try/except
  `httpx.InvalidURL` 的回退（NO_PROXY 里有 `[::1]` 这类带方括号的
  IPv6 时 httpx 建 client 就抛 `Invalid port: ':1]'`，回退为
  `trust_env=False`）删掉了；合并后 4 项下载测试全挂。已按仓库版
  恢复，并把快照同样丢掉的回归测试
  `test_download_survives_bracketed_ipv6_in_no_proxy` 补回
  `tests/test_ai_cores_download.py`。
- `.github/workflows/build-exe.yml`：快照是旧版（无健壮的 ISCC 查找、
  无退出码检查、无 Lite/Setup 条件产物名）；保留仓库版。快照新增的
  `.github/workflows/build-installer.yml` 作为独立工作流保留。

- `installer/AIMS-Backend.iss`：快照是 0.5.9 版（旧 AppId、旧版本号、
  注释里花括号的 ISCC 语法 bug）；保留仓库 1.0.3 修好的版本。
- `build_exe.bat` / `build_onefile.bat`：快照缺 1.0.1+ 的
  webview hidden-import；保留仓库版。
- `requirements-exe.lock`：快照缺 `pefile`、`pywin32-ctypes`；保留仓库版。
- `tests/test_no_console_window.py`：快照是 1.0.0 版，缺 1.0.1/1.0.2
  的窗口化 EXE 启动崩溃回归测试；保留仓库版。
- `CHANGES_0_5_4.md` / `CHANGES_0_5_5.md`：快照是合版前旧文本；保留
  仓库的合版后文本。
- `LICENSES.txt`、`APP_VERSION`：快照分别是 0.5.9 / 1.0.0 版回退；
  保留仓库版并统一升到 1.0.5。
- `I18N_PAIRS` 里 7 条旧词条（AI 中心标签 4 条、“AI Model / Core
  Packs”等 3 条）：快照在重构为 `data-ai-i18n` 驱动后删掉了它们；
  合并时保留（只是查表词条，不影响新版的 key 驱动；删了反而可能让
  仍走 `translateText` 的旧调用丢翻译）。如确认无调用再删。

## 未合入 / 仍未做

- 快照里的 `aims_data/`（运行数据、`.dev_secret_key`、日志）未合入。
- PostgreSQL 全量测试未跑（SQLite 全量见测试报告）。
- 真实 Windows 安装（含内置 PostgreSQL 路径）未验证；推理引擎的
  真实安装（编译 llama-cpp-python）未在沙箱跑过；真实浏览器操作、
  真实设备通讯（Modbus/MQTT）、真实厂商站点云数据下载未验证。

## 测试

- 合并后在本机跑的 SQLite 全量：**660 通过、2 跳过、0 失败**
  （211 秒）。跳过的是 `test_industrial_reads.py:29`（夹具里没有网关）
  与 `test_module_imports.py:59`（缺 xlwt，该包不在 requirements.txt）。
- 合并初版全量曾出现 4 项失败（全在 `test_ai_cores_download.py`），
  原因是快照丢掉了 0.5.5 的 NO_PROXY 回退（见上节）；恢复后重跑全过。
- PostgreSQL 全量未跑；真实 Windows 安装未验证。
