"""Windows 无控制台窗口（GUI 子系统）静态审计测试。

背景：AIMS 正式版是 Windows GUI 子系统程序（PyInstaller `console=False`），
由桌面快捷方式 → AIMS.exe（托盘启动器）→ 隐藏窗口拉起后端与数据库。
**任何不带 CREATE_NO_WINDOW 的控制台子进程调用，都会在用户屏幕上闪出一个
黑色窗口**——这类回归在 Linux CI 上功能测试永远发现不了，只能静态卡住。

本测试做四件事：
1. 用 AST 扫描全部 subprocess / os.system 调用，要求显式带 creationflags，
   或在白名单里（白名单必须写明"为什么这里不需要"，且原因可核对）；
2. 断言 run_standalone.py 不再使用 os.system("pause")（它会弹 cmd 窗口）；
3. 断言打包配置是 GUI 子系统（console=False）；
4. 断言安装器不给用户暴露会弹控制台的快捷方式（.ps1 直连 / cmd 包装）。
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "run_standalone.py", ROOT / "aims_launcher.py"] + sorted(
    (ROOT / "app").rglob("*.py"))

_CALLS = {"run", "Popen", "call", "check_output", "check_call"}

#: 白名单：调用点（文件, 所在函数, 被调方法）→ 为什么这里不需要 CREATE_NO_WINDOW。
#: 每条都必须能在源码里核实，不允许出现"暂时放过"式的空理由。
WHITELIST = {
    ("app/launcher/main.py", "_open_path", "Popen"):
        "只在 POSIX 分支执行（xdg-open）；Windows 走 os.startfile，不产生控制台",
    ("app/launcher/supervisor.py", "start_backend", "Popen"):
        "creationflags 在 kwargs 字典里构建（CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW），"
        "AST 看不到关键字参数；由 test_backend_spawn_hides_console 直接断言 kwargs",
    ("app/services/system_info.py", "_read_macos_meminfo", "run"):
        "只在 macOS 分支执行（sysctl / vm_stat），Windows 走 ctypes GlobalMemoryStatusEx",
}


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """行号 → 最近的外层函数名（用于白名单定位，避免写死行号）。"""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if hasattr(child, "lineno"):
                    owner.setdefault(child.lineno, node.name)
    return owner


def _scan(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    owner = _enclosing_functions(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        # subprocess.run(...) / subprocess.Popen(...) / os.system(...)
        if not isinstance(func.value, ast.Name):
            continue
        mod, method = func.value.id, func.attr
        if mod == "subprocess" and method in _CALLS:
            pass
        elif mod == "os" and method in ("system", "popen"):
            pass
        else:
            continue
        has_flags = any(k.arg == "creationflags" for k in node.keywords)
        found.append({
            "rel": path.relative_to(ROOT).as_posix(),
            "func": owner.get(node.lineno, "<module>"),
            "method": f"{mod}.{method}",
            "line": node.lineno,
            "flags": has_flags,
        })
    return found


def test_no_console_flashing_subprocess_calls():
    offenders = []
    for path in TARGETS:
        for call in _scan(path):
            if call["flags"]:
                continue
            if call["method"] == "os.system" or call["method"] == "os.popen":
                offenders.append(call)  # os.system/popen 不存在"安全"版本，必须改写
                continue
            key = (call["rel"], call["func"], call["method"].split(".")[1])
            if key in WHITELIST:
                continue
            offenders.append(call)
    assert not offenders, (
        "以下子进程调用在 Windows GUI 子系统下会弹出控制台，必须补 "
        "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)：\n"
        + "\n".join(f"  {c['rel']}:{c['line']} in {c['func']}() → {c['method']}" for c in offenders)
    )


def test_whitelist_entries_are_still_real():
    """白名单不能变成"僵尸条目"：被指的函数必须真的还存在、且真的没有 flags。"""
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for path in TARGETS:
        for call in _scan(path):
            key = (call["rel"], call["func"], call["method"].split(".")[1])
            by_key.setdefault(key, []).append(call)
    for key in WHITELIST:
        assert key in by_key, f"白名单条目已失效（函数被改名/删除？）：{key}"


def test_backend_spawn_hides_console():
    """后端子进程必须同时隐藏窗口 + 独立进程组（前者防黑框，后者防 Ctrl 信号误伤）。"""
    src = (ROOT / "app/launcher/supervisor.py").read_text(encoding="utf-8-sig")
    assert "CREATE_NO_WINDOW" in src
    assert "CREATE_NEW_PROCESS_GROUP" in src


def test_no_os_system_pause():
    """`os.system("pause")` 会拉起 cmd.exe 黑窗，GUI 版必须彻底禁用（按 AST 查，避开注释/文档字符串）。"""
    for path in (ROOT / "run_standalone.py", ROOT / "aims_launcher.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        bad = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "system" and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "os"]
        assert not bad, f"{path.name} 仍在使用 os.system（会弹 cmd 窗口），行号：{[n.lineno for n in bad]}"


def test_standalone_pause_uses_native_messagebox():
    """无控制台时用 Windows 原生 MessageBox 停住进程，而不是开控制台。"""
    src = (ROOT / "run_standalone.py").read_text(encoding="utf-8-sig")
    assert "MessageBoxW" in src
    assert "_has_console" in src


def test_debug_console_is_opt_in():
    """调试控制台必须显式开关（AIMS_DEBUG_CONSOLE=1），默认不分配。"""
    src = (ROOT / "run_standalone.py").read_text(encoding="utf-8-sig")
    assert "AIMS_DEBUG_CONSOLE" in src
    assert "AllocConsole" in src
    assert "AttachConsole" in src
    # 默认值必须是"关闭"：比较的字符串里只能出现真值枚举，不能默认放行
    assert 'os.environ.get("AIMS_DEBUG_CONSOLE", "").strip().lower() not in ("1", "true", "yes", "on")' in src


def test_build_is_windows_gui_subsystem():
    spec = (ROOT / "build_exe.spec").read_text(encoding="utf-8-sig")
    assert "console=False" in spec, "后端 EXE 必须是 GUI 子系统，否则双击/启动器拉起时会有黑窗"
    assert "console=True" not in spec
    # PyJWT 迁移后不能还收集 jose（否则打包告警 + 残留 python-jose 依赖）
    assert '"jose"' not in spec and '"jose.jwt"' not in spec


def test_installer_shortcuts_never_open_console():
    iss = (ROOT / "installer/AIMS.iss").read_text(encoding="utf-8-sig")
    # 桌面快捷方式必须直接指向启动器 EXE
    assert 'Name: "{autodesktop}\\AIMS"; Filename: "{app}\\{#LauncherExe}"' in iss
    # 不允许任何快捷方式直接指向 .ps1（点击会弹 PowerShell 控制台）
    for line in iss.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:") and "Filename:" in stripped:
            assert '.ps1"' not in stripped.split("Filename:")[1].split(";")[0], \
                f"快捷方式直连 .ps1 会弹控制台：{stripped}"
    # 备份入口不能直连 powershell.exe（-WindowStyle Hidden 仍会闪一下控制台），
    # 必须经 run-hidden.vbs 用窗口样式 0 启动
    assert "run-hidden.vbs" in iss
    assert "powershell.exe" not in iss.split("[Icons]")[1].split("[Run]")[0]
    # 外部程序调用必须是 runhidden（"立即启动 AIMS" 那条是安装完成后拉起 GUI，不是控制台程序）
    for line in iss.splitlines():
        if line.startswith("Filename:") and "Flags:" in line:
            if "postinstall" in line:
                continue
            assert "runhidden" in line, f"[Run]/[UninstallRun] 必须 runhidden：{line.strip()}"


def test_launcher_exe_is_windowed():
    """AIMS.exe（用户双击的那个）必须是 --windowed 构建。"""
    text = (ROOT / "build/build_common.ps1").read_text(encoding="utf-8-sig")
    section = text.split("function Build-Launcher")[1].split("function Build-Backend")[0]
    assert "'--windowed'" in section, "AIMS.exe 必须用 --windowed 打包，否则双击会带控制台"
    assert "'--console'" not in section


# ---------------------------------------------------------------------------
# 1.0.0 正式版启动即崩溃（2026-11-11 真机）：console=False 后 GUI 子系统
# EXE 的 sys.stdin/stdout/stderr 全是 None，uvicorn 的 ColourizedFormatter
# 在 configure_logging() 里调 sys.stdout.isatty()，config.load() 直接抛
# ValueError，服务线程还没起就死了；用户只看到一个 "Press Enter to exit..."
# 的空弹窗。
#
# 1.0.1 的修复是把缺失的流接到空设备；1.0.2 换成捕获式：_capture_output() 把
# stdout/stderr 包进 _Tee（isatty() 返回 False，uvicorn 同样能配日志），
# 同时把输出留进内存缓冲，崩溃时弹窗直接显示真实原因。以下测试在 Linux 上
# 模拟"流全是 None"的 windowed 环境，把这条因果链锁死（来包的
# test_run_standalone_diagnostics.py 只测捕获本身，不测 uvicorn 这一环）。
# ---------------------------------------------------------------------------


def test_windowed_exe_capture_restores_streams(monkeypatch):
    """流全是 None 时捕获后必须有可用的流（isatty 存在且为 False），print 不崩。"""
    import sys

    import run_standalone

    saved = (sys.stdout, sys.stderr)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        run_standalone._output_buffer.clear()
        run_standalone._capture_output()
        assert sys.stdout is not None and sys.stderr is not None
        assert sys.stdout.isatty() is False  # Tee：不带颜色、不崩溃
        print("x", flush=True)  # 1.0.0 以前这里（及所有 flush=True）都会炸
        sys.stdout.flush()
        sys.stderr.flush()
        assert "x" in run_standalone._output_tail()
    finally:
        sys.stdout, sys.stderr = saved
        run_standalone._output_buffer.clear()


def test_uvicorn_config_loads_without_console(monkeypatch):
    """重现 1.0.0 崩溃：流全是 None 时 uvicorn 配日志必须成功（而不是 ValueError）。"""
    import sys

    import run_standalone

    saved = (sys.stdout, sys.stderr)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        run_standalone._output_buffer.clear()
        run_standalone._capture_output()
        import uvicorn
        from fastapi import FastAPI

        config = uvicorn.Config(FastAPI(), host="127.0.0.1", port=8123, log_level="info")
        config.load()  # 1.0.0 在这里抛 ValueError: Unable to configure formatter 'default'
    finally:
        sys.stdout, sys.stderr = saved
        run_standalone._output_buffer.clear()


def test_pause_dialog_shows_captured_output():
    """弹窗必须显示捕获到的真实报错，绝不能只是一句等回车的提示。"""
    import run_standalone

    run_standalone._output_buffer.clear()
    run_standalone._output_buffer.append(
        "服务线程异常：\nValueError: Unable to configure formatter 'default'\n"
    )
    try:
        text = run_standalone._pause_dialog_text("Press Enter to exit...")
        assert "Unable to configure formatter" in text
        assert "Press Enter to exit..." in text
        assert text.strip() != "Press Enter to exit..."
    finally:
        run_standalone._output_buffer.clear()


def test_output_buffer_is_bounded(monkeypatch):
    """长期运行的服务持续写日志，捕获缓冲必须封顶（否则内存无限涨）。"""
    import sys

    import run_standalone

    saved = (sys.stdout, sys.stderr)
    try:
        run_standalone._output_buffer.clear()
        run_standalone._capture_output()
        for i in range(2000):
            print(f"access-log-line-{i:04d} " + "x" * 60)
        total = sum(len(s) for s in run_standalone._output_buffer)
        assert total <= run_standalone._OUTPUT_LIMIT_CHARS
        tail = run_standalone._output_tail()
        assert "access-log-line-1999" in tail, "封顶后仍要保留最新的输出"
    finally:
        sys.stdout, sys.stderr = saved
        run_standalone._output_buffer.clear()
