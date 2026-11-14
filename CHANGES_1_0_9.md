# AIMS 1.0.9 变更说明（2026-11-14）

在 1.0.8 基础上做两件事：模型下载页新增**独立推理引擎安装**（打包后的
EXE 也能装），未登录时**高级功能页直接锁定**（页面内显示锁定屏，不再
跳去登录页）。

## 模型下载页：独立推理引擎安装（EXE 可用）

- **问题**：模型下载页原来的引擎安装是 pip 装 llama-cpp-python，
  打包后的 EXE 里这条路被明确拒绝（`/ai/packages/.../install` 在
  `sys.frozen` 时返回 400），所以在真正装机的那台 Windows 上，
  “安装”按钮永远装不上，只能改 `requirements-exe-ai.txt` 重新打包。
- **新做法**：新增独立引擎安装（`app/services/engine_installer.py`），
  从 ggml-org/llama.cpp 的 GitHub 发布页下载官方预编译 Windows 二进制
  （`llama-server.exe`），解压到可写的数据目录
  `<MODELS_DIR>/engines/<engine-id>/`。不用 pip、不用重打包，源码模式和
  EXE 安装版都能装。
- **三个引擎**（Windows x64，模型下载页“独立推理引擎”区，超管才能装）：
  - CPU 版（`llama-cpu`）：`llama-bNNNNN-bin-win-cpu-x64.zip`，约 25 MB，
    任何 x64 电脑都能用，推荐首选；
  - NVIDIA CUDA 12.4 版（`llama-cuda124`）：主包约 320 MB，**另需**
    `cudart-llama-bin-win-cuda-12.4-x64.zip` 伴生包约 390 MB（CUDA 包
    只带 `ggml-cuda.dll`，不带 NVIDIA 运行时，缺了伴生包启动不了），
    合计约 710 MB，界面上先写明；
  - Vulkan 版（`llama-vulkan`）：约 38 MB，AMD / Intel 显卡可用，不用 CUDA。
- **版本不写死**：发布选择查 GitHub Releases 列表，取第一个带对应包的
  `bNNNNN` 夜间版——`/releases/latest` 现在指向只有 `nightly-tag.txt`
  的 `v1.0.0`，不能用它（2026-11-14 实测最新夜间版 `b12375`）。
- **安全**：下载地址只认 GitHub 官方域名（引擎 id 是固定白名单，不是
  用户给的 URL）；每个包都按 GitHub 发布的 `sha256:` 摘要校验，校验
  不过直接丢弃；解压防 zip-slip（绝对路径/`..`/符号链接一律拒绝）、
  有大小与文件数上限；先解到 `.tmp` 目录、确认 `llama-server.exe`
  在里面后才替换旧引擎目录。安装/卸载接口均为超级管理员专用。
- **验证**：Windows 上装完会跑一次 `llama-server.exe --version`；跑不通
  不判安装失败，但状态里如实记“未验证运行”，界面上照实显示。
- **界面**：模型下载页新增“独立推理引擎（EXE 安装版可用）”区（安装/
  卸载按钮、下载大小、安装状态）；原来的 pip 方式改标为“Python 包安装
  （仅源码 / 开发模式）”，仍在模型引擎页保留；“模型状态”面板也认
  独立引擎（已安装即显示已安装，未验证运行会注明）。
- **注意**：独立引擎是单独的 `llama-server.exe` 进程，目前只做到“装上
  可用”，还没有把 `llm_local` 的推理改成去调它——那是下一步，不混
  在这版里。
- 新接口：`GET /ai/engines`、`POST /ai/engines/{id}/install`、
  `DELETE /ai/engines/{id}?confirm=true`；`GET /ai/engine` 增加
  `native_engines` / `native_installed` 字段。

## 未登录：高级功能页直接锁定

- **问题**：以前未登录点受保护页会直接跳去“系统设置 → 登录”，人要
  先反应过来自己在哪、再登录、再找回原页；数据分析页（AI 解读、导入、
  图表）还是高级功能，却一直没锁，未登录也能打开看。
- **新做法**：
  - 锁定范围补上**数据分析**（`danal`）：高级功能页现在是 AI 中心、
    数据分析、知识库、模板、维护、资产、实验室、云数据、系统设置，
    全部锁定。仪表盘 / 设备 / 通讯 / 告警 / 数据是基础浏览页，未登录
    时只显示演示数据，仍可看（服务端接口本来就有鉴权，真数据拿不到）。
  - **原地锁定**：未登录打开受保护页时，不再跳走，该页原地显示锁定屏
    ——大 🔒 + 页面名 + “请先登录后使用此页面”，下面一个“去登录”按钮；
    页面本身的内容不渲染。登录后自动回到该页。会话过期时正停在受保
    护页，同样原地变锁定屏；深链（`#ai` 等）直达受保护页也一样。
  - 侧栏的 🔒 标记、登录后回原页、强制改密码的拦截都不变。
- 新词条：`pageLockTitle` / `pageLockDesc`（中英文）。

## 测试

- 新增 `tests/test_engine_installer.py`（14 个）：三个引擎的包名与
  b12375 真实发布资产名对得上；版本选择跳过只有 `nightly-tag.txt`
  的 `v1.0.0`、取到 `b12375`；CUDA 配对 cudart 伴生包；zip-slip 与
  符号链接拒绝；本地假发布服务器上的安装/卸载往返、sha256 校验失败
  拒绝、未知引擎与非 Windows 拒绝；接口层列出引擎、未知引擎 400、
  卸载必须 `confirm=true`。下载客户端带与 `ai_cores` 同款的
  NO_PROXY 回退（`NO_PROXY` 里有 `[::1]` 时 httpx 建 client 会抛
  `Invalid port: ':1]'`，回退为不读环境代理）——这正是本地实测时
  抓到的真问题，已修。
- `tests/test_frontend_gating.py`：锁定集合加入 `danal`；新增锁定屏
  测试（锁定屏元素、原地锁定、登录后回原页、会话失效与深链也锁定）；
  模型下载页切页断言同步新行（含 `loadNativeEngines()`）。
- 未验证（需真机 / 真环境）：真实 Windows EXE 上下载安装三个引擎
  （尤其 CUDA 约 710 MB 的下载与 `llama-server.exe --version` 实跑）、
  真实浏览器里点锁定屏与登录回跳、PostgreSQL 全量、真实 Windows 安装。
