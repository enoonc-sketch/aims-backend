# AIMS 1.0.4 变更说明（2026-11-12）

1.0.4 是 1.0.3 的 i18n 深化版：前端翻译从“词条”升级到“逐字配对 + 动态文案
全覆盖”，另修两处功能缺陷（新 TAG 绑网关 500、云数据源 Playwright 通用错误
500）与迁移幂等；不做功能加法。

## 前端翻译深化（frontend.html）

- **侧栏改纯 key 驱动**：1.0.3 已用 `data-i18n` 补齐 alarm/tpl 两项修掉错位；
  本版把 `updateAllUIText` 里按序号映射的 pageIds 数组整个拿掉，侧栏按钮
  文字完全由各自的 `data-i18n="pageTitle_*"` 驱动，新增/漏写一项也不会再
  整体错位；测试 `test_nav_buttons_are_key_driven` 防回退。
- **静态文案逐字配对**：新增 `I18N_PAIRS`（302 条中英对照）+ 快照/还原引擎
  （`_i18nSnapshot` / `_i18nApply` / `_i18nRerenderDynamic`）。启动时记住
  每个文本节点原文，`changeLang` 时双向切换；覆盖云数据、数据管理、数据分析、
  AI 中心、知识库、维护、资产、实验室、模板中心、系统等页面的说明、表头、
  占位符、提示。以前“云数据”整页是写死的中文，英文界面下全是中文；数据分析
  页以前只会翻成中文、切回英文不还原。
- **提示语不再夹英文菜单名**：`登录已失效，请重新登录（System → Login）` →
  中文界面显示“系统设置 → 登录”（会话失效文案改走 `aimsExpiredMsg()` +
  `t('sessionExpired')`）。会话弹窗按钮、上传前检查、导入结果/失败、413/429
  等提示全部接入词条。
- **动态文案改 `L(en, zh)`**：云数据页 / 数据管理页云同步部分的表格按钮、
  状态、日志、覆盖率、断点等；第二轮补齐模板中心（列表/详情/版本/导入预览/
  导入结果/导入记录及全部弹窗）、报警列表与规则、系统（用户/审计/组件）、
  数据管理上传结果与趋势图提示、故障 PDF 解析结果、AI 对话错误提示、AI 预测/
  健康度提示；切换语言时当前页自动重绘。`showPage` 英文标题兜底里的
  “Alarms · 报警”“Templates · 模板中心”混排已去掉。
- **修正**：知识库页第一个标签页被误设成“搜索知识库...”长句（改回“搜索 /
  Search”）；`tf()` 对参数中的 `$&` 等字符的误替换（改函数替换值）。
- 词条 44 → 76（en/zh 各，键集合一致）；`data-i18n` 出现 28 → 32 次。
- 有意不翻译：服务端返回的数据（如模板分类名“设备台账”）、`#sopType` 的
  Calibration/Replacement/Maintenance（option 无 value，文字即提交值）、
  技术名词（ID、csv、Bias %、MAPE %）、用户数据。

## 功能缺陷修复

1. **给新 TAG 绑定网关 + 点位地址返回 500**（`POST /devices/{id}/components`）。
   `get_or_create_tag` 内部已 `link_gateway`（待写入、未 flush），接口里又调用
   一次；会话是 `autoflush=False`，`db.get()` 看不到待写入对象，重复 INSERT
   撞 `gateway_devices` 唯一约束。这是“TAG→通讯”的主路径，不绑网关就没法用
   网关推送入库。修复：`link_gateway` 同时检查 `db.new`。
   它能逃过 618 个测试的原因：`tests/conftest.py` 的会话用了默认
   `autoflush=True`，与生产不一致。新回归测试
   `tests/test_functional_regressions.py` 用 `autoflush=False` 的会话，在旧
   代码上稳定复现。
2. **云数据源“测试登录 / 刷新”遇到证书错误、域名解析失败、连接被拒时返回
   500 + 原始堆栈**。Playwright 这类错误是通用 `Error` 而非 `TimeoutError`，
   原先只捕获后者；函数文档却承诺“任何失败都转成 ScraperError”。修复：
   登录、下载、启动浏览器三处都转成带中文原因的 `ScraperError`（接口返回
   400）。
3. `app/api/v1/ai.py`：`select(distinct(col))` →
   `select(col).where(...).distinct()`，消除 SAWarning。

## 迁移幂等（PostgreSQL 16 + pgvector 验证）

- `alembic upgrade head` 在全新空库上必挂（`007_industrial_core`、
  `009_security_hardening` 用裸 `op.create_table` / `op.create_index`，而
  `001_initial` 已按当前模型 `create_all`，表早已存在，报 DuplicateTable）。
  修复：两个迁移改为幂等（先检查再建，与 002-006、008 一致），downgrade
  同样加了存在性保护。回归测试 `tests/test_migrations_fresh_db.py`（SQLite
  即可复现；对原始迁移失败、对修复后通过）。
- 来包在 PostgreSQL 上验证：应用自动建表（57 张表，
  `knowledge_chunks.embedding` 为真正的 `vector(1536)`）、`tools/e2e_smoke.py`
  189 项全过、七个批量导入链路与 SQLite 结果一致；pgvector 检索用假的
  OpenAI 兼容 embeddings 服务验证通过。未验证：TimescaleDB 超表（沙箱未安装，
  代码对其为可选）、在 PostgreSQL 上运行 pytest 全套（测试夹具固定用 SQLite
  内存库）。
- 新增 `tools/e2e_smoke.py`：可重复运行的端到端检测脚本（只能对一次性测试
  实例运行，用法见文件头）。

## 交付包清理

- `requirements.txt` 增加 `reportlab>=4.2.5,<6`（`test_fault_pdf.py` 需要，
  仅测试用）；新增 `pytest.ini`，设置
  `asyncio_default_fixture_loop_scope = function`。
- `tests/test_security_hardening.py` 两处 `from jose import jwt` →
  `import jwt`；`app/core/security.py` 注释同步改为 PyJWT。

## 测试

新增 `tests/test_frontend_i18n.py`（5 例：en/zh 词条键一致、所有用到的词条
已定义、侧栏由 key 驱动、中文提示不含写死的英文菜单名、`I18N_PAIRS` 格式
正确）、`tests/test_functional_regressions.py`（4 例，参数化展开为 6 项）、
`tests/test_migrations_fresh_db.py`（2 例）；全量 **631 passed / 2 skipped /
0 failed**（1.0.3 是 618；新增 13 项参数化展开）。

## 合版说明（对照已发布 1.0.3 核实）

本版底包是已发布 1.0.3（提交 bd71ed0），只从 AIMS-1_0_0-Source-i18n-fix.zip
（CHANGES_I18N_FIX.md）移植 i18n 增量与上述修复；该包是旧底平行包，**绝不
能直接用**，合版时从已发布 1.0.3 恢复/保留了：NO_PROXY 修复及回归测试
（`app/services/ai_cores.py` 的 `trust_env=False` 回退；
`test_download_survives_bracketed_ipv6_in_no_proxy`）、修好的
`installer/AIMS-Backend.iss`（GUID 花括号注释 bug + 已发布 AppId
`{2B5C694D-...}`，只升版本号到 1.0.4）、`_capture_output` 的缓冲封顶
64KB 与 `test_no_console_window.py` 的 4 项捕获式回归测试、首次登录凭据、
diagnose CLI、`.gitignore` 的 build/、build/ 签名实现、CHANGES_0_5_10.md、
CHANGES_0_6_0.md、CHANGES_1_0_1.md、CHANGES_1_0_2.md、CHANGES_1_0_3.md、
`package_release.ps1` 的签名后哈希、`requirements-exe.lock` 的
pefile/pywin32-ctypes、bat 的 webview hidden-import。`frontend.html` 用三路
合并（1.0.1 树作共同祖先）逐块移植 I18N_PAIRS + 快照引擎 + L() + 第二轮
动态文案 + tf() 的 `$&` 修复 + 侧栏纯 key 驱动，8 处冲突全部取新包侧；合
并结果与新包前端逐字节一致，且 1.0.3 的前端内容（data-i18n、tf()、aims_lang
持久化、401 重登录按钮、20 秒组件超时）全部保留或被新机制等价替换。
`aims_data/logs/` 的 5 个诊断日志未合入。`.github/workflows/build-exe.yml`
以网页上的加固版为准，不从包里抄；两安装器（AIMS.iss 与 AIMS-Backend.iss）
互不识别的老问题未动，只升版本号。

## 未验证

Windows 真机新版启动/安装/升级/卸载、签名流程、真实浏览器里的新翻译观感、
真实 PostgreSQL 上的全套 pytest、TimescaleDB 超表、`tools/e2e_smoke.py`
需真实后端。
