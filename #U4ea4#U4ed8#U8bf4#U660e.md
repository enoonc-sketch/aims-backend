# AIMS 桌面化改造 · 交付说明

本文件是本次交付的总入口。按"我要做什么"找对应章节。

---

## 一、这次交付了什么

源码位置：`/workspace/aims-backend-0.5.9-offline`（在原 0.5.9 源码基础上增量改造，未删任何业务功能）

| 类别 | 内容 | 位置 |
|---|---|---|
| 部署引擎 | 内置 PostgreSQL 集群管理、建库建表、扩展安装、结构对账、备份恢复、自检 | `app/deploy/`（13 个模块） |
| 桌面启动器 | 托盘、单实例、服务编排、父进程看门狗、开机自启、诊断报告、AI 预检 | `app/launcher/` + `aims_launcher.py` |
| 功能自检 | 30 项真实业务请求，一条命令出验收级报告 | `deploy selftest`（`app/deploy/selftest.py`） |
| 安装程序 | Inno Setup 6，含覆盖升级、回滚、保数据卸载、安装后健康检查 | `installer/AIMS.iss` |
| 构建系统 | 依赖检查、x64/ARM64 构建、发行打包、下载校验 | `build/*.ps1`（5 个脚本） |
| 运维脚本 | 启动/停止/备份/恢复/修复/状态/卸载/计划任务/验收 | `tools/*.ps1`（10 个脚本） |
| 文档 | 部署、用户手册、故障排查、功能对照、依赖与许可证、测试报告 | `docs/`（6 篇）+ `LICENSES.txt` |

改动摘要见 `CHANGES_0_5_9_DESKTOP.md`；逐条缺陷与修复见 `docs/测试报告.md` §4。

---

## 二、安装后的样子

```
C:\Program Files\AIMS\               程序（只读）
  AIMS.exe                           桌面启动器（托盘）
  AIMS-Backend\AIMS-Backend.exe      后端（内嵌 Python，含 llama.cpp）
  frontend.html                       单文件界面（无 CDN）
  cores\                              规则包 + SheetJS/Chart.js
  pgsql\                              内置 PostgreSQL 16 + pgvector
  tools\                              运维脚本
  rollback\<旧版本>\                  升级回滚副本

C:\ProgramData\AIMS\                 数据（升级/卸载默认都不动）
  Config\  Database\  Models\  Logs\  Uploads\  Backup\
```

用户操作只有三步：**双击安装包 → 双击桌面图标 → 用托盘菜单退出**。
不需要 Python、Node.js、Docker、PostgreSQL，也不需要联网。

---

## 三、已验证 / 未验证（重要，请勿混淆）

### 已在真实环境执行并通过（Ubuntu + PostgreSQL 16.15 + pgvector 0.8，真实 HTTP 请求）

| 项 | 结果 |
|---|---|
| 全新部署（程序/数据分离） | 通过，7.8 秒 |
| 端口冲突自动避让 | 通过（55432 被占 → 55433） |
| 建表 + pgvector + BRIN | 通过 |
| 启动器编排（DB→AI→后端→界面） | 通过（1.1 秒就绪） |
| **30 项功能自检** | **30/30 通过** |
| 备份 / 恢复（含安全备份、SHA-256） | 通过（恢复后标记数据消失、原数据完整） |
| 覆盖升级 | 通过（`created_tables=0`，纯原地升级） |
| 卸载保留数据 / 重装 | 通过（数据 68 MB 保留，重装后行数不变） |
| 孤儿进程防护 | 通过（强杀启动器后后端 3 秒自退） |
| 单元测试 | 455 passed, 1 skipped |

### 未在目标环境执行（**因此本版本不标记为"正式交付"**）

| 项 | 原因 | 执行方法 |
|---|---|---|
| 安装包编译（ISCC） | 需要 Windows + Inno Setup 6 | 构建机运行 `build\build_x64.ps1` |
| 干净 Win11 安装/启动/功能/卸载 | 无 Windows 环境 | 目标机运行 `tools\Windows-Acceptance.ps1 -Setup <安装包>` |
| 安装包方式的覆盖升级 | 需要两个版本安装包 | 同上，加 `-UpgradeSetup` |
| ARM64 | 无 ARM64 硬件 | 见 `docs/部署说明.md` §8；**未真机验证前不得宣称兼容** |

`Windows-Acceptance.ps1` 覆盖 20+ 个检查点（静默安装、文件齐全、目录分离、快捷方式、
安装后自检、服务注册、启动、界面、自检、备份、校验、停服、恢复、恢复后自检、升级、
升级后自检、卸载、残留进程、服务注销、数据保留、程序清理），产出 JSON + Markdown 报告。

---

## 四、如实说明的两个技术限制

### 1. TimescaleDB 无法打包（但不影响功能）

TimescaleDB 官方**没有 Windows 构建**，也没有可自由再分发的 Windows 二进制。处理方式：

- 没有 TimescaleDB 时 `measurements` 保持普通表，自动建 **BRIN 索引**（时序数据的标准替代），功能与查询能力不缺失；
- 一旦把 TimescaleDB 的 Windows 扩展文件放进 `pgsql\share\extension` 与 `pgsql\lib`，
  再运行 `tools\Repair-Aims.ps1`，下次启动会自动 `CREATE EXTENSION` 并转 hypertable，**无需改代码、无需迁移数据**。

（需求书第八阶段规则："如果发现某项功能无法打包，必须说明具体原因、依赖和解决方案" —— 以上即说明。）

### 2. ARM64 的数据库是 x64 仿真

PostgreSQL 官方同样没有 Windows ARM64 版本。默认产出 `-arm64-emulation-Setup.exe`：
应用为 ARM64（在 ARM64 构建机构建时），数据库以 x64 仿真运行（性能 -10~25%）。
`build_arm64.ps1 -Native` 在缺少 ARM64 数据库时会**拒绝**生成"原生"包，避免名不副实。

---

## 五、下一步怎么做（建议顺序）

```powershell
# 1) 在 Windows 构建机上出安装包
cd <源码>\build
.\dependency_check.ps1 -ForBuild
.\build_x64.ps1 -UpdateLock          # 首次会下载内置 PostgreSQL 并记录 SHA-256
.\package_release.ps1 -Installer ..\installer_output\AIMS-0.5.10-x64-Setup.exe

# 2) 在干净 Win11 x64（断网）上验收
#    拷入 release\AIMS-0.5.10\ 整个目录
.\tools\Windows-Acceptance.ps1 -Setup .\AIMS-0.5.10-x64-Setup.exe `
    -Arm64:$false -ReportDir .\acceptance

# 3) 把 acceptance\acceptance-report.md 追加到 docs\测试报告.md 附录
#    全部通过后才可对外标记为正式交付版本
```

---

## 六、文档索引

| 文档 | 读者 | 内容 |
|---|---|---|
| [docs/部署说明.md](docs/部署说明.md) | 实施/运维 | 布局、安装、升级、备份恢复、配置、构建、端口、ARM64、TimescaleDB |
| [docs/用户手册.md](docs/用户手册.md) | 终端用户 | 启动退出、托盘、首次登录、功能地图、备份、数据位置、FAQ |
| [docs/故障排查.md](docs/故障排查.md) | 运维/支持 | 按阶段分类的症状-原因-处理表，诊断信息收集 |
| [docs/功能对照表.md](docs/功能对照表.md) | 产品/验收 | 18 模块 136 接口逐项对照，证明功能零删减 |
| [docs/依赖与许可证清单.md](docs/依赖与许可证清单.md) | 法务/架构 | 组件、版本、用途、许可证、架构矩阵 |
| [LICENSES.txt](LICENSES.txt) | 法务 | 随安装包分发的许可声明 |
| [docs/测试报告.md](docs/测试报告.md) | 验收方 | 实测结果、缺陷清单、未执行项与执行方法 |
