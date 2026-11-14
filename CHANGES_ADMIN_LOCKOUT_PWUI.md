# 默认账号 / 登录锁定 / 修改密码界面

1. 安装版默认账号改为 admin/admin（与桌面独立版一致）；生产环境仍拒绝默认账号。自定义密码（--admin-password）与旧安装不受影响。
   修复：provision 以前拿到的“是否新建管理员”标志永远是 False，first-login.txt 从未写出，现已修复。
2. 登录锁定按“来源 IP + 用户名”组合计数；单 IP 累计 20 次失败的上限仍保留（防用户名喷洒）。
3. 前端：系统设置新增“修改密码”标签页（中英文），调用 /auth/change-password，成功后保存新令牌。
4. 测试：SQLite 全量 654 通过、2 跳过（test_industrial_reads.py:29 夹具里没有网关；test_module_imports.py:59 缺 xlwt，该包不在 requirements.txt）。
5. 英文界面下模板分类名（20 个，如“设备台账”→ Equipment Ledger）改为显示层翻译：分类栏、模板卡片、详情、导入预览、新建/编辑下拉；
   传给后端的值（data-cat / option value）仍是中文原值。切换语言时模板页自动重绘并保持所选分类。测试：test_template_category_names_all_have_english_labels。
   未完成：部署说明/用户手册/安装说明/AIMS.iss 注释里仍有“随机密码”旧描述；PostgreSQL 全量未跑。
   2026-11-13 补：上述四处文档/注释的“随机密码”旧描述已改为默认 admin/admin（Muse）；仍未做：PostgreSQL 全量未跑、真实 Windows 安装（含内置 PostgreSQL 路径）未验证。
