"""前端：登录门禁、AI 中心标签页与推理引擎页、默认账号提示（静态解析 frontend.html）。

这些都是真实出过的问题：
- AI 中心标签页按位置映射文案，加了“选型”之后整体错位，“LLM 对话”被叫成“模型下载”；
- “安装推理引擎”按钮用裸 fetch、不带令牌，登录了管理员也必定 401；
- doLogout 里局部变量 t 遮住了翻译函数 t()，点“退出登录”会抛 “t is not a function”。
"""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "frontend.html").read_text(encoding="utf-8")


def _func(name: str) -> str:
    m = re.search(r"(?:^|\n)(?:async )?function %s\(.*?\n\}\n" % re.escape(name), HTML, re.S)
    assert m, f"找不到函数 {name}"
    return m.group(0)


# ------------------------------------------------------------------ AI 中心
def test_ai_tabs_are_key_driven_and_distinct():
    assert "aiTabs[" not in HTML, "不要再按位置映射 AI 标签页"
    tabs = re.findall(r"""<button class="tab-btn[^"]*" onclick="switchAiTab\('(\w+)',this\)"([^>]*)>""", HTML)
    assert [t for t, _ in tabs] == ["diag", "pred", "health", "sel", "chat", "models", "engine"]
    keys = []
    for _, attrs in tabs:
        m = re.search(r'data-ai-i18n="(\w+)"', attrs)
        assert m, "AI 标签页缺 data-ai-i18n"
        keys.append(m.group(1))
    for lang_marker in ("tabSel: 'Selection'", "tabSel: '选型'"):
        i = HTML.index(lang_marker)
        block = HTML[max(0, i - 2500): i + 200]
        vals = [re.search(r"\b%s: (?:'([^']*)'|\"([^\"]*)\")" % k, block) for k in keys]
        assert all(vals), (lang_marker, keys)
        names = [v.group(1) or v.group(2) for v in vals]
        assert len(set(names)) == len(names), f"AI 标签页名称重复：{names}"


def test_chat_tab_is_called_model_status_and_models_tab_has_engine():
    assert "tabApply: '模型状态'" in HTML or 'tabApply: "模型状态"' in HTML
    assert "LLM对话" not in HTML.split("const translations")[0].split("<body")[-1]
    assert "tabModels: '模型下载'" in HTML or 'tabModels: "模型下载"' in HTML
    assert "tabEngine: '模型引擎'" in HTML or 'tabEngine: "模型引擎"' in HTML
    assert "模型下载 / 推理引擎" not in HTML, "下载与引擎已经拆成两个标签页，不应再有合并名"
    assert 'id="aiSecEngine"' in HTML and 'id="pyPackagesList"' in HTML and 'id="aiEngineBanner"' in HTML


def test_model_download_and_engine_are_separate_tabs():
    """“模型下载”页只留核心包卡片；引擎安装区（状态条 + 安装包列表 + 依赖）搬进独立的“模型引擎”页，
    打开引擎页才加载安装包状态，下载页不再顺带加载。"""
    models_at = HTML.index('id="ai-models"')
    engine_at = HTML.index('id="ai-engine"')
    know_at = HTML.index('id="page-know"')
    models_panel = HTML[models_at:engine_at]
    engine_panel = HTML[engine_at:know_at]
    assert 'id="aiModelsList"' in models_panel and 'id="aiSecModels"' in models_panel
    assert 'id="aiSecEngine"' not in models_panel and 'id="pyPackagesList"' not in models_panel
    assert 'id="aiSecEngine"' in engine_panel and 'id="pyPackagesList"' in engine_panel
    assert 'id="aiEngineBanner"' in engine_panel and 'id="pyDepsList"' in engine_panel
    switch = _func("switchAiTab")
    assert "if (tab === 'models') { refreshAiModels(); loadPyPackages(); loadNativeEngines(); }" in switch
    assert "if (tab === 'engine') { loadPyPackages(); }" in switch
    assert "loadPyPackages();" not in switch.split("if (tab === 'models')")[0]
    # 模型下载页另有一块“本地模型推理引擎”下载区（只列引擎构建），与引擎页共用同一批安装卡片
    assert 'id="aiModelsEngineList"' in models_panel and 'id="aiModelsEngineSec"' in models_panel
    assert "aiModelsEngineList" in _func("renderPyPackages")
    assert "switchAiTab('engine',this)" in HTML and "aiGoEngineTab()" in HTML


def test_engine_install_is_authenticated():
    body = _func("installPyPackage")
    assert "aimsFetch(" in body
    assert "fetch(apiBase()" not in body, "安装接口要求超级管理员令牌，不能用裸 fetch"


# ------------------------------------------------------------------ 强制改密码
def test_default_password_forces_password_change():
    """默认口令登录必须先改密码：登录后跳去修改密码，其余页面和设置标签页全部挡住，
    改完密码才恢复。标志由后端在登录 / 刷新 / /auth/me 里给出，前端只做跳转和拦截。"""
    store = _func("aimsStoreTokens")
    assert "must_change_password" in store, "令牌里要同步后端的 must_change_password 标志"
    clear = _func("aimsClearTokens")
    assert "aimsSetMustChangePassword(false)" in clear, "退出登录要清掉强制改密码标志"
    assert "aims_must_change_pw" in _func("aimsMustChangePassword")
    assert "aims_must_change_pw" in _func("aimsSetMustChangePassword")

    show = _func("showPage")
    assert "aimsMustChangePassword()" in show and "aimsGoToChangePassword()" in show
    assert "id !== 'sys'" in show, "强制改密码时只放行系统设置页（修改密码在里面）"

    sys_tab = _func("switchSysTab")
    assert "aimsMustChangePassword()" in sys_tab and "tab !== 'password'" in sys_tab

    login = _func("doLogin")
    assert "aimsGoToChangePassword()" in login, "默认口令登录后要去修改密码，而不是回待定页面"
    assert "must_change_password" in login, "登录后要以后端 /auth/me 的标志为准"

    change = _func("doChangePassword")
    assert "aimsStoreTokens(data)" in change, "改完密码的新令牌要清掉标志"
    assert "aimsRefreshLocks()" in change, "改完密码要把被藏起来的设置标签页放出来"

    assert "pwMustChange" in HTML
    go = _func("aimsGoToChangePassword")
    assert "aimsOpenChangePassword()" in go and "pwMustChange" in go


def test_engine_section_tells_user_to_start_backend():
    """后端连不上时，推理引擎区不能只写“无法连接后端”，要说清先启动后端。"""
    assert "请先启动后端服务" in HTML
    assert "Start the backend service first" in HTML
    body = _func("loadPyPackages")
    assert "请先启动后端服务" in body
    assert "aiRenderEngineBanner(null, msg)" in body


# ------------------------------------------------------------------ 系统设置页走后端
def test_settings_page_uses_backend_api_and_i18n():
    """设置页的两个值存在后端 /system/settings；打开设置页读后端，连不上才用本机；
    保存走 PUT；页面上没有写死的英文提示，所有文案走词条。"""
    settings_at = HTML.index('id="sys-settings"')
    section = HTML[settings_at:settings_at + 900]
    for key in ("sysSettingsTitle", "apiEndpointLabel", "refreshIntervalLabel", "saveSettingsBtn"):
        assert 'data-i18n="%s"' % key in section, f"设置页缺 data-i18n={key}"
    assert "Settings saved!" not in HTML, "不要再写死英文保存提示，走词条"

    load = _func("loadSettings")
    assert "aimsFetch('/system/settings')" in load
    assert "localStorage.getItem('aims_api_endpoint')" in load, "后端连不上时用本机保存的端点"
    assert "localStorage.getItem('aims_refresh_interval')" in load, "后端连不上时用本机保存的间隔"
    assert "ui.api_endpoint" in load and "ui.refresh_interval" in load

    save = _func("saveSettings")
    assert "method: 'PUT'" in save and "'/system/settings/'" in save
    assert "ui.api_endpoint" in save and "ui.refresh_interval" in save
    assert "settingsInvalidInterval" in save, "间隔要校验（5–3600 整数秒）"
    assert "settingsSavedLocal" in save, "后端连不上要如实说只保存在本机"
    assert "settingsNeedAdmin" in save, "没权限保存要明说需要管理员账号"
    assert "Settings saved!" not in save

    switch = _func("switchSysTab")
    assert "loadSettings();" in switch.split("if (tab === 'settings')")[1].split("\n")[0], "打开设置页要读后端保存的值"
    """角色页给超级管理员一个改密码入口，与“修改密码”标签页调同一个接口，
    改的是当前登录账号自己的口令；普通用户看不到它。"""
    roles_at = HTML.index('id="sys-roles"')
    users_at = HTML.index('id="sys-users"')
    roles = HTML[roles_at:users_at]
    assert 'id="sysRolePw"' in roles
    for fid in ("rolePwCurrent", "rolePwNew", "rolePwConfirm", "rolePwBtn", "rolePwResult"):
        assert 'id="%s"' % fid in roles
    assert 'onclick="doRoleChangePassword()"' in roles
    assert 'data-i18n="pwTitle"' in roles

    body = _func("doRoleChangePassword")
    assert "_postChangePassword(cur, nw)" in body, "角色页改密码要与修改密码标签页走同一条请求"
    assert "aimsStoreTokens(data)" in body, "改完密码要存新令牌"
    shared = _func("_postChangePassword")
    assert "/auth/change-password" in shared
    assert "aimsFetch(" in shared

    render = _func("aimsRenderSession")
    assert "sysRolePw" in render and "is_superuser" in render, "只有超级管理员（用户管理权限）能看到角色页的改密码入口"
    funcs = list(re.finditer(r"(?:^|\n)(?:async )?function (\w+)\(([^)]*)\)\s*\{", HTML))
    bad = []
    for i, m in enumerate(funcs):
        end = funcs[i + 1].start() if i + 1 < len(funcs) else len(HTML)
        seg = HTML[m.start():end]
        shadow = re.search(r"\b(var|let|const)\s+t\s*=", seg) or re.search(r"(^|,)\s*t\s*(,|$)", m.group(2))
        if shadow and re.search(r"""(?<![\w.])t\(\s*['"]""", seg):
            bad.append(m.group(1))
    assert not bad, f"这些函数里局部变量 t 遮住了翻译函数 t()：{bad}"


# ------------------------------------------------------------------ 登录门禁
def test_login_gate_covers_advanced_pages():
    m = re.search(r"var AIMS_LOCKED_PAGES = \[([^\]]*)\]", HTML)
    assert m
    locked = set(re.findall(r"'(\w+)'", m.group(1)))
    # 高级功能页全部锁定：AI 中心、知识库、模板、维护、资产、实验室、云数据、
    # 系统设置，外加数据分析（AI 解读/导入/图表也是高级功能）。
    assert {"ai", "know", "tpl", "maint", "asset", "lab", "ext", "sys", "danal"} <= locked
    assert not ({"dash", "dev", "comm", "alarm", "data"} & locked)   # 基础页面保持可看
    show = _func("showPage")
    assert show.index("aimsPageLocked(id)") < show.index("classList.remove('active')"), "门禁必须在切页之前"
    assert "aimsAfterLogin();" in _func("doLogin")          # 登录后回到原来想去的页面
    assert "aimsKickIfLocked();" in _func("aimsSessionExpired")   # 退出/失效后踢回登录页
    assert ".nav-btn.locked::after" in HTML


def test_locked_page_shows_lock_screen_in_place():
    """未登录打开受保护页面：原地显示锁定屏（🔒 + 页面名 + 去登录），而不是
    直接跳去登录页；页面本身的内容不渲染；登录后自动回到该页。"""
    assert 'id="aimsPageLock"' in HTML
    lock_at = HTML.index('id="aimsPageLock"')
    lock_html = HTML[lock_at:lock_at + 800]
    assert "🔒" in lock_html
    assert "aimsGoToLogin()" in lock_html, "锁定屏上有去登录的按钮"
    assert "aimsPageLockTitle" in lock_html and "aimsPageLockDesc" in lock_html
    show = _func("showPage")
    assert "aimsShowPageLock(id)" in show, "未登录切受保护页要原地显示锁定屏"
    assert "aimsGoToLogin()" not in show.split("aimsPageLocked(id)")[1].split("}")[0], \
        "锁定分支不应再直接跳去登录页"
    lock_fn = _func("aimsShowPageLock")
    assert "_aimsPendingPage = id" in lock_fn, "锁定屏要记住待定页面，登录后回去"
    assert "aimsPageLock" in lock_fn and "pageLockTitle" in lock_fn
    assert "classList.remove('active')" in lock_fn, "锁定屏显示时页面内容要先藏起来"
    assert "aimsHidePageLock()" in _func("aimsAfterLogin"), "登录后要先撤掉锁定屏再跳回待定页"
    assert "aimsShowPageLock(pid)" in _func("aimsKickIfLocked"), "会话失效时也要原地锁定"
    assert "aimsShowPageLock(page)" in _func("aimsApplyDeepLink"), "深链直达受保护页也要原地锁定"
    assert 'pageLockTitle: "“{0}” is locked"' in HTML or "pageLockTitle: '“{0}” is locked'" in HTML
    assert 'pageLockTitle: "「{0}」已锁定"' in HTML or "pageLockTitle: '「{0}」已锁定'" in HTML


def test_default_account_hint_and_prefill():
    assert 'id="loginUser" value="admin"' in HTML
    assert 'data-i18n="loginDefaultHint"' in HTML
    assert "默认账号 / 密码：admin / admin123" in HTML
    assert "Default account / password: admin / admin123" in HTML
    for needle in ("op123", "maint123", "sys123"):     # 旧的演示口令仍不得出现在前端
        assert needle not in HTML


# ------------------------------------------------------------------ 数据分析页不留小字提示
def test_data_analysis_page_has_no_helper_hints():
    """数据分析页去掉静态小字提示：上传区格式说明、X 轴提示、统计卡片小字、
    状态行初始文案、图表区占位文案；标签、按钮、输入框和实时数值本身保留。"""
    removed_zh = ["支持 .xlsx / .xls / .csv 格式", "浏览器安全策略要求点击选择文件",
                  "通常选择“时间”，也可选择任意数值/文本列。", "请选择Excel", "不含表头",
                  "数值列 0", "等待中", "请导入Excel文件。", "导入文件并选择曲线后在此显示。"]
    removed_en = ["Supports .xlsx / .xls / .csv", "Browser security requires clicking to select files.",
                  'Usually select "Time", or any numeric/text column.', "Please select Excel",
                  "Excluding header", "Numeric 0", "Waiting", "Please import Excel file.",
                  "Import file and select curves to display here."]
    for s in removed_zh + removed_en:
        assert s not in HTML, f"数据分析页仍有小字提示：{s}"
    for el in ("daMFileSub", "daMNumCols", "daMRange"):
        assert el not in HTML, f"统计卡片小字元素应已删除：{el}"
    # 标签、按钮、输入框和实时数值本身保留；状态行留空，等实时状态再填。
    for el in ("daMFile", "daMRows", "daMCols", "daMFiltered", "daStatus", "daChart",
               "daDrop", "daXSelect", "daRenderBtn"):
        assert f'id="{el}"' in HTML, f"数据分析页缺元素：{el}"
    assert 'id="daStatus" class="da-status"></div>' in HTML


# ------------------------------------------------------------------ 侧栏底部登录状态
def test_sidebar_login_status_indicator():
    """侧栏底部登录状态：紧挨“系统状态”；未登录显示“未登录”，登录后显示
    “已登录：用户名”；点一下去登录（已登录时退出登录）；登录/退出/切换语言
    都会刷新（aimsRenderSession 在这几处都被调用）。"""
    status_at = HTML.index('id="sysStatus"')
    sidebar = HTML[status_at:status_at + 400]
    assert 'id="loginStatus"' in sidebar
    assert 'id="loginStatusText"' in sidebar
    assert 'onclick="aimsLoginStatusClick()"' in sidebar
    assert 'data-i18n="loginStatusOut"' in sidebar
    # 中英文词条齐全。
    assert 'loginStatusIn: "Signed in: {0}"' in HTML
    assert 'loginStatusIn: "已登录：{0}"' in HTML
    assert 'loginStatusOut: "Not signed in"' in HTML
    assert 'loginStatusOut: "未登录"' in HTML
    render = _func("aimsRenderSession")
    assert "loginStatusText" in render
    assert "loginStatusIn" in render and "loginStatusOut" in render
    # 颜色：已登录浅绿，未登录红色；初始（未登录）就是红色。
    assert "'#73d13d'" in render and "'#ff4d4f'" in render
    assert "color:#ff4d4f" in sidebar
    click = _func("aimsLoginStatusClick")
    assert "doLogout()" in click, "已登录时点一下退出登录"
    assert "aimsGoToLogin()" in click, "未登录时点一下去登录"


# ------------------------------------------------------------------ 托盘“设置”深链 #sys/settings
def test_settings_deep_link_hash():
    """托盘“设置”用 #sys/settings 直达设置页：登录后跳页+切标签，未登录先记
    下来去登录，登录成功后 aimsAfterLogin 再跳过去。"""
    assert "function aimsApplyDeepLink()" in HTML
    dl = _func("aimsApplyDeepLink")
    assert "location.hash" in dl
    assert "showPage(page" in dl and "switchSysTab(tab" in dl
    assert "_aimsPendingTab" in dl
    after = _func("aimsAfterLogin")
    assert "_aimsPendingTab" in after


# ------------------------------------------------------------------ 1.0.8：模型状态 / 设置页账号权限 / 页面权限 / 托盘上报
def test_model_status_tab_has_status_panel_and_keeps_chat():
    chat_at = HTML.index('id="ai-chat"')
    models_at = HTML.index('id="ai-models"')
    panel = HTML[chat_at:models_at]
    assert 'id="aiModelStatusBox"' in panel and 'id="aiModelStatusRows"' in panel
    assert 'id="chatBox"' in panel and "sendChat()" in panel          # 对话功能还在
    assert "aiLoadModelStatus();" in _func("loadChatModelOptions")


def test_data_analysis_page_has_no_da_hint_elements():
    page = HTML[HTML.index('id="page-danal"'):HTML.index('id="page-ai"')]
    assert 'class="da-hint"' not in page and "daSheetHint" not in HTML
    assert "Sum of 19 components" not in page and "Major: Nitrogen" not in page


def test_settings_tab_has_account_and_permission_sections():
    sec = HTML[HTML.index('id="sys-settings"'):HTML.index("</body>")]
    for el in ("acctMine", "acctAdmin", "acctUser", "acctPass", "acctRole", "acctBody", "permAdmin", "permRole", "permGrid"):
        assert f'id="{el}"' in sec, el
    assert "if (tab === 'settings') { loadSettings(); aimsLoadAccountSettings(); }" in _func("switchSysTab")
    for fn in ("doCreateAccount", "permSave", "permRender", "acctSetRole"):
        assert f"function {fn}(" in HTML


def test_page_permission_gate_runs_before_switching_page():
    show = _func("showPage")
    assert "aimsPageDenied(id)" in show
    assert show.index("aimsPageDenied(id)") < show.index("classList.remove('active')")
    denied = _func("aimsPageDenied")
    assert "id === 'sys'" in denied          # 系统页（改密码/退出）永远可进


def test_ui_state_is_reported_on_token_changes():
    for fn in ("aimsStoreTokens", "aimsClearTokens", "aimsRenderSession"):
        assert "aimsReportUiState()" in _func(fn), fn
    assert "'/auth/ui-state'" in _func("aimsReportUiState")
