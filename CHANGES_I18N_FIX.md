# 中英文翻译修复 + 交付包问题清理

## 一、中英文翻译（frontend.html）
- **侧栏错位**：旧版 `updateAllUIText` 用写死的 id 数组按序号给侧栏按钮赋文字，数组缺了 alarm/tpl 两项，
  整列错位两格，末尾冒出重复的英文 `Cloud Data` / `System`。现在完全由每个按钮自带的 `data-i18n` 驱动，
  不再按位置映射；并有测试 `test_nav_buttons_are_key_driven` 防回退。
- **静态文案双向翻译**：新增 `I18N_PAIRS`（约 300 条中英对照）+ 快照/还原引擎。启动时记住每个节点原文，
  `changeLang` 时双向切换；覆盖云数据、数据管理、数据分析、AI 中心、知识库、维护、资产、实验室、模板中心、
  系统等页面的说明、表头、占位符、提示。以前“云数据”整页是写死的中文，英文界面下全是中文；
  数据分析页以前只会翻成中文、切回英文不还原。
- **提示语不再夹英文菜单名**：`登录已失效，请重新登录（System → Login）` → 中文界面显示“系统设置 → 登录”。
  会话弹窗按钮、上传前检查、导入结果/失败、413/429 等提示全部接入词条。
- **云数据页 / 数据管理页云同步部分的动态文案**（表格按钮、状态、日志、覆盖率、断点等）改用 `L(en, zh)`；
  切换语言时当前页会自动重绘。
- **第二轮（动态文案补齐）**：模板中心（列表/详情/版本/导入预览/导入结果/导入记录及全部弹窗）、
  报警列表与规则、系统（用户/审计/组件）、数据管理上传结果与趋势图提示、故障 PDF 解析结果、AI 对话错误提示、
  AI 预测/健康度提示，全部改为随界面语言切换；`showPage` 英文标题兜底里的 “Alarms · 报警”“Templates · 模板中心” 混排已去掉。
- 修正：知识库页第一个标签页被误设成“搜索知识库...”长句；`tf()` 对参数中的 `$&` 等字符的误替换。
- 有意不翻译：服务端返回的数据（如模板分类名“设备台账”）、`#sopType` 的 Calibration/Replacement/Maintenance（option 无 value，文字即提交值）、
  技术名词（ID、csv、Bias %、MAPE %）、用户数据。

## 二、交付包问题
1. `build/` 目录：本包已包含，发布链检查（`test_release_chain.py`）通过。
2. `tests/test_security_hardening.py` 两处 `from jose import jwt` → `import jwt`；`app/core/security.py` 注释同步改为 PyJWT。
3. `requirements.txt` 增加 `reportlab`（`test_fault_pdf.py` 需要）；新增 `pytest.ini`，设置
   `asyncio_default_fixture_loop_scope = function`。
4. `app/api/v1/ai.py`：`select(distinct(col))` → `select(col).where(...).distinct()`，消除 SAWarning。

## 三、新增测试
`tests/test_frontend_i18n.py`：en/zh 词条键一致、所有用到的词条已定义、侧栏由 key 驱动、
中文提示不含写死的英文菜单名、`I18N_PAIRS` 格式正确。

## 四、功能完整性检测中发现并修复的缺陷
检测方式：启动真实后端 → 逐个调用全部 199 个路由（95 个 GET 全部可用）→ 189 项端到端业务流程 →
前端 79 处接口调用与后端路由逐一对账（0 悬空）→ 用前端对着真实后端登录并加载 14 个页面。

1. **给新 TAG 绑定网关 + 点位地址返回 500**（`POST /devices/{id}/components`）。
   `get_or_create_tag` 内部已 `link_gateway`（待写入、未 flush），接口里又调用一次；会话是 `autoflush=False`，
   `db.get()` 看不到待写入对象，重复 INSERT 撞 `gateway_devices` 唯一约束。这是“TAG→通讯”的主路径，
   不绑网关就没法用网关推送入库。修复：`link_gateway` 同时检查 `db.new`。
   **它能逃过 618 个测试的原因**：`tests/conftest.py` 的会话用了默认 `autoflush=True`，与生产不一致。
   新回归测试 `tests/test_functional_regressions.py` 用 `autoflush=False` 的会话，在旧代码上稳定复现。
2. **云数据源“测试登录 / 刷新”遇到证书错误、域名解析失败、连接被拒时返回 500 + 原始堆栈**。
   Playwright 这类错误是通用 `Error` 而非 `TimeoutError`，原先只捕获后者；函数文档却承诺“任何失败都转成
   ScraperError”。修复：登录、下载、启动浏览器三处都转成带中文原因的 `ScraperError`（接口返回 400）。
3. 新增 `tools/e2e_smoke.py`：可重复运行的端到端检测脚本（只能对一次性测试实例运行，用法见文件头）。

## 五、PostgreSQL 16 + pgvector 0.6 验证与迁移修复
- 在 PostgreSQL 上：应用自动建表（57 张表，`knowledge_chunks.embedding` 为真正的 `vector(1536)`）、
  `tools/e2e_smoke.py` 189 项全过、七个批量导入链路与 SQLite 结果一致。
- pgvector 检索：用假的 OpenAI 兼容 embeddings 服务验证——字面零重叠、语义相同的查询能命中，
  语义无关的查询不会误命中；PostgreSQL 取回的向量是 list，`rag.py` 的向量路径正常。
- **缺陷 3：`alembic upgrade head` 在全新空库上必挂**（`007_industrial_core`、`009_security_hardening`
  用裸 `op.create_table` / `op.create_index`，而 `001_initial` 已按当前模型 `create_all`，表早已存在，
  报 DuplicateTable）。README / SECURITY.md 都要求用户执行该命令，但之前没有任何测试跑过 Alembic。
  修复：两个迁移改为幂等（先检查再建，与 002-006、008 一致），downgrade 同样加了存在性保护。
  升级后与应用自动建表的结构逐表逐列比对：一致（仅有两个性能索引——向量索引、时间 BRIN——
  由应用启动的 Postgres extras 步骤创建，不在迁移里）。
  回归测试：`tests/test_migrations_fresh_db.py`（SQLite 即可复现；对原始迁移失败、对修复后通过）。
- 未验证：TimescaleDB 超表（沙箱未安装，代码对其为可选）。

## 六、测试可在 PostgreSQL 上整套运行
`tests/conftest.py` 本来就支持 `TEST_DATABASE_URL`，但只切了夹具，应用自身的 `DATABASE_URL` 仍是 SQLite
内存库（半截状态）。现在应用的 `DATABASE_URL` 在测试里强制跟随 `TEST_DATABASE_URL`，既让切换只需设一个变量，
也保证开发机环境里指向真实库的 `DATABASE_URL` 不会被测试碰到（teardown 会 drop_all）。

    # 需要一个已安装 pgvector 的 PostgreSQL；库内容会被测试建表并在结束时清空
    TEST_DATABASE_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/aims_test pytest

实测：SQLite 与 PostgreSQL 16 上均为 626 通过、2 跳过（跳过的是依赖数据条件的 `pytest.skip`）。

## 七、默认账号、登录门禁、AI 中心“模型应用 / 模型下载 / 推理引擎”
1. **默认账号密码 admin / admin**：不注入任何账号配置、空库启动时自动创建（开发/桌面环境）。
   `ALLOW_DEV_ADMIN` 未设置时由“默认关闭”改为“非生产默认开启”，`DEV_ADMIN_PASSWORD` 默认 `admin`；
   **生产环境仍然一律拒绝**（强制开启会拒绝启动）。HTTP 接口的密码策略不变（改密/建用户仍不接受 `admin`），
   只有首次创建与 CLI（`create-admin` / `reset-password`）对这一组默认值放行。库里遗留的 `admin/admin`
   在 `ALLOW_DEV_ADMIN=false` / 生产环境启动时会被自动停用。已有数据库的账号不会被改动。
   原先默认关闭时，桌面独立版空库启动后没有任何可登录账号。
2. **登录门禁**：未登录进不了 AI 中心、知识库、模板中心、维护管理、资产管理、实验室、云数据、系统设置
   （侧栏显示 🔒）。点击后跳到「系统设置 → 登录」并提示要用哪个页面；系统设置页未登录时只开放“登录”标签，
   其余（角色/设置/用户/审计/组件）隐藏；登录成功自动回到原来想去的页面；退出登录或会话失效时若停在受保护页面，
   自动送回登录页。仪表盘、设备、通讯、报警、数据管理、数据分析仍可在未登录时查看（演示数据）。
   受保护页面清单只在 `AIMS_LOCKED_PAGES` 一处，要增减改这个数组即可。
   登录表单预填用户名 admin，并显示“默认账号 / 密码：admin / admin”提示。
3. **AI 中心标签页**：原先 5 个标签的按位置映射在加了“选型”之后整体错位，中文下出现两个“模型下载”
   （其中第一个其实是 LLM 对话）。现在改为按 key 驱动：
   诊断 | 预测 | 健康评分 | 选型 | **模型应用**（原 LLM 对话） | **模型下载 / 推理引擎**。
   “模型应用”页新增“去下载模型 / 安装引擎”入口。
4. **“模型下载 / 推理引擎”页**：顶部新增推理引擎状态条（已安装 / 未安装，带两个锚点链接）；下分“模型下载”
   与“推理引擎”两个区块，推理引擎区只列 llama.cpp 的 CPU / CUDA 12.1 / CUDA 12.4 三个版本，
   numpy / scikit-learn / statsmodels 另列“其他分析依赖”。
5. **修复“安装推理引擎”按钮必定 401**：接口要求超级管理员，前端却用裸 `fetch` 不带令牌；改用 `aimsFetch`。
6. **修复“退出登录”会报错**（原版就有）：`doLogout` 里局部变量 `t` 遮住翻译函数 `t()`，
   抛 “t is not a function”，本地令牌不会清除。新增静态检查，扫描全脚本不再有同类遮蔽。
7. 新增测试：`tests/test_frontend_gating.py`；`tests/test_security_hardening.py` 按新策略更新并补充
   （生产仍拒绝、API 仍拒绝弱口令、遗留 admin/admin 在不允许时被停用）。

**需要你知道的取舍**：默认 admin/admin 是公开的弱口令，只适合本机单用户/内网试用；对外提供服务前请
改密码，或设置 `ALLOW_DEV_ADMIN=false` 后用 `python -m app.cli create-admin` 建管理员。
当前前端没有“修改密码”界面（后端 `/auth/change-password` 有），改密暂时只能走接口或 CLI。
Windows 安装包的 provision 流程仍是安装时生成随机管理员口令并写入 first-login.txt，本次没有改动。
