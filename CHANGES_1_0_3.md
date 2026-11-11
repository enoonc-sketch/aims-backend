# AIMS 1.0.3 变更说明（2026-11-11）

1.0.3 是 1.0.2 的可用性版：首次登录凭据可发现 + 前端中文翻译修完，
不做功能加法。

## 首次登录凭据：随机初始密码可发现、可读（2026-11-11，现场反馈）

- 产品版本就没有 `admin/admin` 默认口令——首个管理员由 `provision` 用
  `secrets.token` 随机生成 16 位初始密码（避开易混淆字符），但口令只存在
  `Config\secrets.json`（JSON，人读体验差），用户拿 admin/admin 反复试错只会
  得到「用户名或密码不正确」；
- 新增 `conf.write_first_login_credentials()`：首次安装真实创建了管理员后，把
  用户名 + 初始密码 + 登录入口 + 「改密后删除本文件」提示写入
  `Config\first-login.txt`（0600，与 secrets.json 同目录同 ACL）；
- `init_db` 新增 `BOOTSTRAP_ADMIN_CREATED` 标志：只有真的新建了账号才写凭据，
  升级 / 已有用户时不写，避免把改过的旧密码再次落盘误导用户；
- 安装器：完成页新增「**查看首次登录凭据（用户名与初始密码，务必记下）**」
  postinstall 勾选项（记事本打开凭据文件）；开始菜单新增「AIMS 首次登录凭据」；
- `deploy provision --admin-password`（已有）仍可自定义初始密码；老安装用
  `AIMS-Backend.exe reset-password admin` 重置；`docs/部署说明.md` 第 3 节
  新增「首次登录凭据」小节；
- 新增 `tests/test_first_login_credentials.py`（5 例）。

## 前端中文翻译：错位与缺漏修复

- **侧边栏错位（真 Bug）**：`updateAllUIText` 的 pageIds 少了 `alarm`/`tpl` 两项，
  按索引映射导致第 4 个按钮起整体错位两格——「报警」显示成「数据管理」、
  「实验室」显示成「云数据」，而「云数据 / System」保持英文。改为 data-i18n
  属性驱动（与 changeLang 统一），并补齐 `pageTitle_alarm`/`pageTitle_tpl` 词条；
- 系统设置页签 Users / Audit Log / Components 补齐中文（用户 / 审计日志 / 组件），
  Login / Roles / Settings 改用独立词条不再与登录表单按钮共用；
- 登录流程全部文案接入 i18n：用户名或密码不正确、账号停用、服务器拒绝、输入无效、
  响应超时、后端不可达、429 锁定倒计时、Show/Hide、Caps Lock 提示、退出登录提示等
  （新增 `tf()` 带占位符翻译函数）；
- 语言选择持久化到 localStorage（`aims_lang`），下次打开自动恢复，语言下拉框同步；
- 状态栏「System Status: Online」、登录框占位符、Signed in as / Sign out 同步翻译。

## 测试

新增 `tests/test_first_login_credentials.py` 5 例；全量 **618 passed /
2 skipped**（1.0.2 是 613；新增 5）。来包未写全量数字，见实测。

## 合版说明（对照已发布 1.0.2 核实）

本包底包基于 1.0.2 源码，合版时从已发布 1.0.2 恢复了：NO_PROXY 修复及
回归测试、CHANGES_0_5_4/0_5_5 合并版、CHANGES_0_5_10.md、CHANGES_0_6_0.md、
CHANGES_1_0_1.md 与 CHANGES_1_0_2.md、加固版 build-exe 工作流（示例标签改
到 v1.0.3）、修好的 AIMS-Backend.iss（GUID 花括号 bug + 已发布 AppId，
版本号 1.0.3）、bat 的 webview hidden-import、lock 的 pefile/pywin32-ctypes、
LICENSES.txt 版本。本包自己的改动全部保留：首次登录凭据、前端中文翻译、
build/ 目录（本包自带原版签名实现，另有 .gitignore 修复：此前整目录被忽略，
导致 git archive 导出的源码包丢失打包链路）。另改三处：package_release.ps1
的清单哈希改取签名后的交付件（来包原版取的是签名前原文件，签完哈希就对
不上了——顺序陷阱 2 的真 bug）；`_capture_output` 的缓冲封顶 64KB（同
1.0.2）；test_no_console_window.py 的 4 项回归测试按捕获式设计重写（同
1.0.2，来包不含这 4 项）。安装说明里三处"1.0.0 已修/已修复/起"改成 1.0.3
（这些修复不在已发布的 1.0.0 里）。工作流文件不能用 push-files 推，
须在网页上改。
