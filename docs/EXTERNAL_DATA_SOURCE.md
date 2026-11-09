# 数据管理 / 外部数据源抓取模块

对接厂商云平台（登录 + 左侧十几个数据库，逐个下载）、导入到系统、检测数据连续性、
自动联动报警/维护。新增文件一览：

```
app/models/external_source.py     ExternalSource / ExternalDatabase / ExternalSyncLog / DataContinuityGap
app/schemas/external.py           对应的请求/响应模型
app/services/crypto_util.py       登录密码可逆加密（存库用）
app/services/external_scraper.py  Playwright 模拟登录 + 点击下载
app/services/external_import.py   把下载的文件解析并导入 measurements
app/services/data_continuity.py   数据断点检测 + 报警/工单联动
app/services/external_sync_service.py  串起 登录->下载->导入->巡检->日志 一次完整同步
app/services/external_scheduler.py     APScheduler 定时刷新
app/api/v1/external_data.py       REST 接口
```

## 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium   # 下载 Chromium 内核，只需执行一次
```

`EXTERNAL_DOWNLOAD_DIR`（默认 `data/external_downloads`）、`PLAYWRIGHT_HEADLESS`
（默认 `true`，调试选择器时可以在 `.env` 里改成 `false` 弹出真实浏览器窗口观察）
两个配置项在 `app/core/config.py`。

## 2. 配置一个厂商站点（ExternalSource）

十几个数据库共用同一套账号密码登录一次，所以先建一个 Source：

1. 打开目标网站登录页，按 F12 打开开发者工具，分别在账号输入框、密码输入框、
   登录按钮上"检查元素"，记下它们的 CSS 选择器（例如 `#username`、
   `input[type=password]`、`button.login-btn`）。
2. 找一个登录成功后才会出现的元素（比如导航栏里的用户名、首页某个 id），记下
   选择器，填到 `login_success_selector` —— 这是判断"登录到底成不成功"的依据，
   强烈建议填，不填只能用"页面等到网络空闲"这种弱判断。
3. 调用：

```http
POST /api/v1/external/sources
{
  "name": "示例分析仪云平台",
  "base_url": "https://vendor.example.com",
  "login_url": "https://vendor.example.com/login",
  "username": "your_account",
  "password": "your_password",
  "username_selector": "#username",
  "password_selector": "#password",
  "submit_selector": "button.login-btn",
  "login_success_selector": "#nav-username"
}
```

4. 测试登录（不下载任何数据，只验证选择器对不对）：

```http
POST /api/v1/external/sources/{id}/test-login
```

失败会直接返回具体原因（哪个选择器找不到 / 超时），据此调整配置重试。

## 3. 逐个配置十几个"数据库"（ExternalDatabase）

每个数据库对应"登录后跳到哪个页面 -> 需不需要先选时间范围/点查询 ->
点哪个下载按钮"：

```http
POST /api/v1/external/databases
{
  "source_id": 1,
  "name": "1号分析仪在线数据",
  "list_url": "https://vendor.example.com/data/device1",
  "pre_download_steps": [
    {"action": "fill",  "selector": "#start_date", "value": "2026-09-01"},
    {"action": "fill",  "selector": "#end_date",   "value": "2026-09-27"},
    {"action": "click", "selector": "#query_btn"},
    {"action": "wait",  "selector": "#result_table"}
  ],
  "download_selector": "text=导出",
  "data_format": "csv",
  "device_code_column": "设备编号",
  "component_column": "组分",
  "time_column": "时间",
  "value_column": "数值",
  "auto_refresh_enabled": true,
  "refresh_interval_minutes": 60,
  "expected_interval_minutes": 10
}
```

如果某个数据库导出的文件本来就只对应一台设备/一个组分（文件里没有设备编号列），
用 `default_device_code` / `default_component_name` 代替对应的 `*_column` 即可。
列名留空时会按常见的中英文表头自动识别（`device_code`/设备编号、`time`/时间等），
需要注意：**`device_code` 必须是设备管理里已存在的设备编号**，不存在的会被跳过
并计入 `warnings`，不会自动建设备。

`data_format` 支持 `csv` / `xlsx` / `json`（json 接受纯数组，或
`{"data": [...]}` 这种外面包一层的结构）。

## 4. 手动刷新 / 查看结果

```http
POST /api/v1/external/databases/{id}/refresh     # 同步执行一次，返回本次结果
GET  /api/v1/external/databases/{id}/logs        # 历史抓取记录（成功/失败/行数）
GET  /api/v1/external/databases/{id}/continuity  # 这个数据库名下的断点
GET  /api/v1/external/gaps?device_id=...         # 全局数据连续性总览
```

## 5. 数据怎么流转

下载文件 -> `external_import.py` 解析 -> 写入 `measurements` 表时复用现有的
`app/services/data_chain.py`（设备 -> TAG -> 报警规则判断），也就是说外部数据
一旦落库，走的是和手动导入、实时上报完全一样的链路，AI 中心/趋势图/报警都能
直接用。

## 6. 数据连续性巡检

每次同步后，对这次实际写入的 (设备, 组分) 扫描最近 30 天内的时间序列：
相邻两条记录间隔超过 `expected_interval_minutes` 的 2 倍记为 `info`，4 倍记为
`warning`，10 倍记为 `critical`；`warning`/`critical` 会自动生成一条 Alarm，
并按现有报警->维护链条（`app/services/alarms_hooks.py`）自动开工单——也就是
"网站没数据了"会直接体现在报警和维护模块里，不需要单独巡检页面。

## 7. 可维护性指标 & 与实验室挂钩

```http
GET /api/v1/external/databases/{id}/health
    # 抓取成功率、连续失败次数、最近成功时间、未处理断点数
GET /api/v1/external/coverage?device_id=1&component_name=O2&hours=168&expected_interval_minutes=10
    # 覆盖率、断点数、最长断点、last_data_at、currently_stale（是否已停更）
GET /api/v1/lab/data-coverage?device_id=1&component_name=O2&window_minutes=30
    # 每条化验样品采样时刻前后窗口内有没有在线数据；没有的会标出是哪个断点
    # (explained_by_gap_id) 造成的——实验室对比/AI 认证"配不上对"的常见原因
```

## 8. 前端页面（frontend.html → 左侧 “Data Mgmt / 数据管理”）

- **数据源**：填站点登录信息和选择器，保存后点“测试登录”；密码不回显。
- **数据库**：配置每个数据库的页面地址、下载按钮、列映射、自动刷新间隔；列表里有
  “刷新”（手动同步）、“日志”（每次抓取的行数/错误）、成功率与连续失败次数。
- **数据连续性**：断点列表（可标记已处理）；选设备+组分后查看数据覆盖率，
  并显示化验样品在线数据覆盖情况（对应 `/lab/data-coverage`）。

页面使用与其他页面相同的登录令牌（System 页登录后生效），后端不可达时会在表格里显示错误。

## 9. 已知限制

- 定时任务用 APScheduler 跑在本进程内，只在这个后端进程存活时生效；如果这个
  服务以多副本部署，会重复调度，需要改成单独的 worker 进程。
- 抓取完全依赖配置的 CSS 选择器，厂商站点改版后需要同步更新
  `ExternalSource`/`ExternalDatabase` 的选择器配置，代码本身不用改。
- `refresh` 接口是同步执行的（真实开一个无头浏览器），网络慢或站点响应慢时
  这个请求可能要等待数十秒，前端调用时请设置足够的超时时间。

## 附：登录问题修复说明

System 页显示 “Login successful … (offline demo mode)” 且后端日志里大量 `401 Unauthorized`，
原因是种子管理员账号的密码哈希与 `admin123` 不匹配，真实登录一直失败，前端又把失败当成
“后端不可达”走了离线演示分支（不会产生令牌）。已修复：新库用 `hash_password("admin123")` 生成；
旧库在后端启动时自动修复（仅当密码仍是那个错误哈希时才改，自己改过的密码不会动）。
**重启后端即可**，然后在 System 页用 admin / admin123 重新登录，并刷新页面。
