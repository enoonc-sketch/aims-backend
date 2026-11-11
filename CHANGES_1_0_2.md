# AIMS 1.0.2 变更说明（2026-11-11）

1.0.2 是 1.0.1 的排障能力版：1.0.0 现场反馈"装完打不开"与"登录已失效"，
本版既补排障能力，也修掉两个真实成因，不做功能加法。

## 启动失败诊断（1.0.0 现场反馈"装完打不开"）

- **启动输出捕获**（`run_standalone.py`）：进程启动即把 stdout/stderr 包进
  `_Tee` 复制进内存缓冲（原流照常收到输出，调试控制台/文件日志/cmd 管道
  不受影响）；崩溃弹窗正文 = 捕获到的输出 + 提示语，用户第一次就能读到
  失败原因。`sys.stdout` 为 None（GUI EXE 常态）时 print 也不崩；
  `_Tee.isatty()` 返回 False，uvicorn 配日志正常通过（1.0.0 崩溃的根因
  在此一并解决）。缓冲封顶 64KB（长期运行的服务持续写日志，不封顶会涨内存）。
- **一键自诊断**（`app/deploy/diagnose.py`，`AIMS-Backend.exe diagnose`）：
  生成 `aims-diagnose-latest.txt`：关键判断（把事实翻译成病因）+ 运行环境 +
  路径与权限（存在/可写）+ 生效配置（口令脱敏）+ 数据库状态（托管模式、
  pg_ctl、是否初始化、postmaster PID、是否接受连接、端口占用）+ 各日志尾部。
  启动失败的四个分支（数据库启动失败、端口被占、15 秒未就绪、配置/启动异常）
  以及顶层未捕获异常都会自动生成该报告；GUI 弹窗关闭后自动打开报告所在文件夹。
  报告只输出白名单变量，`DATABASE_URL` 密码段与含 PASSWORD/SECRET/TOKEN 的值
  一律 `***`，可安全外发。
- **线程静默死亡无痕迹**：uvicorn 的 `Server.run` 在 lifespan 启动失败时调用
  `sys.exit(1)`——在工作线程里是静默死亡，主线程只看到"线程不再存活"。
  新增 `_install_thread_hook()`，把线程内的 SystemExit 与异常（含堆栈）打到
  输出，GUI 模式下会被捕获进弹窗。
- **集群位置未落盘**（真实成因，`app/deploy/conf.py`）：安装生成的
  `Config\aims.env` 只写了 `AIMS_PG_PORT`，没写 `AIMS_PG_DATA` /
  `AIMS_PG_BINDIR` / `AIMS_DATA_ROOT`。若 provision 时用了非默认数据目录
  （换盘、迁移、独立数据盘），重启后没人知道集群在哪，程序去默认路径找
  `PG_VERSION` 找不到，报"数据目录不存在"——现象与原因完全对不上。现已全部落盘，
  配置文件自身即可描述部署形态。
- **引导顺序错误**（真实成因，`app/deploy/runtime.py`）：`bootstrap()` 原先
  先判断"是否托管安装"（按默认路径看 `pgsql/data` 是否存在）再读 `aims.env`，
  导致配置里的 `AIMS_PG_DATA` 在判断之后才生效：自定义数据目录的安装会被判成
  "非托管"，整个数据库引导被跳过（轻则打不开，重则静默以空 SQLite 库启动）。
  现改为 `_apply_config_paths()` 先把配置中的路径应用到环境，再做形态判断；
  真实环境变量仍然优先（支持工程师可临时指向另一套库）。
- 《安装说明》新增第八章「启动失败三步定位法」（弹窗即原因 / 调试控制台 /
  aims_crash.log 查找顺序）。

## 系统设置 → Components「登录已失效」（2026-11-11，现场反馈）

- **跨页签并发刷新把整族令牌吊销**（真实成因）。桌面窗口 + 浏览器两个页签
  同时拿同一份 refresh token 刷新时，后到的按"重放"处理、整族吊销——用户毫无
  征兆被登出。后端新增并发宽容窗口（`AIMS_REFRESH_GRACE_SECONDS`，默认 5 秒）：
  条件 UPDATE 竞态（真并发）在窗口内同族再签发，窗口外的顺序重放维持整族吊销
  （重放检测回归测试原样通过）。前端在刷新前先看 localStorage——别的页签刚
  换新过就直接复用，不再发第二次请求。真实 HTTP 并发实测：两请求均 200；
  顺序重放仍 401 + 全族死。实现细节：重读消费时间必须 `db.refresh(row)`——
  身份映射里是读行时的旧快照，不强制刷新就永远判不到赢家的提交。
- **Components 页签的死路提示**（真实成因之二）。会话失效时只显示一句红字，
  没有可操作的出口；且 `/system/components` 首次要递归统计 Models/Backup/
  Database 目录占用（多 GB 时数秒），超过前端 4 秒兜底超时被掐断。修复：
  该请求超时放宽到 20 秒并显示采集提示；401 时渲染「重新登录」按钮一键跳转；
  目录占用带 300 秒 TTL 缓存；`import llama_cpp`（打包 EXE 里秒级）结果进程级
  缓存。实测端点 3–4 ms。
- **目录占用统计静默失效**（隐藏 Bug）。`Settings` 没有 `AIMS_DATA_ROOT` 字段，
  `getattr(settings, "AIMS_DATA_ROOT", None)` 恒为 None——功能从未生效过。改用
  部署布局 `load_layout().base` 取数据根。
- 前端刷新失败时的错误消息统一为可操作的中文（原先是英文 "session expired"）。

## 测试

新增 `tests/test_diagnose.py` 11 例（脱敏、分节、报告落盘、布局残缺不崩、
配置落盘、引导顺序、健康安装零告警）+ `test_run_standalone_diagnostics.py`
8 例；`test_security_hardening.py` +3（竞态宽容、可关闭、顺序重放仍吊销）、
`test_system_components.py` +3（缓存命中/计算入缓存/llama 缓存）；
`test_no_console_window.py` 的 4 项 1.0.0 崩溃回归测试按捕获式设计重写
（uvicorn 配日志仍是锁死点）。全量 **613 passed / 2 skipped**
（1.0.1 是 588；新增 25 = 诊断 11 + 捕获 8 + 安全 3 + 组件 3；
重写的 4 项原地替换，不计新增）。

## 合版说明（对照已发布 1.0.1 核实）

本包底包基于 1.0.0 源码，合版时从已发布 1.0.1 恢复了：NO_PROXY 修复及
回归测试、CHANGES_0_5_4/0_5_5 合并版、CHANGES_0_5_10.md、CHANGES_0_6_0.md
与 CHANGES_1_0_1.md、加固版 build-exe 工作流（示例标签改到 v1.0.2）、
修好的 AIMS-Backend.iss（GUID 花括号 bug + 已发布 AppId，版本号 1.0.2）、
bat 的 webview hidden-import、lock 的 pefile/pywin32-ctypes、LICENSES.txt
版本、build/ 目录（5 个 .ps1；本包缺失，签名测试与 build-installer.yml
都要用到它）。另改两处：`_capture_output` 的缓冲封顶 64KB；安装说明里三处
"1.0.0 已修/已修复/起"改成 1.0.2（这些修复不在已发布的 1.0.0 里）。
工作流文件不能用 push-files 推，须在网页上改。
