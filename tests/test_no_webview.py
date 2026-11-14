"""1.0.11: 界面只用电脑自带的 Edge --app 窗口 —— 代码、打包配置、安装器里
不得再出现 pywebview / WebView2 / pythonnet / clr_loader。

历史文档（CHANGES_*、docs/计划书对照里的 1.0.11 之前记录）不在此列；
这里只卡会进 EXE 的东西。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BANNED = re.compile(r"webview|pythonnet|clr_loader", re.IGNORECASE)
_SCANNED = [
    *_ROOT.glob("app/**/*.py"),
    _ROOT / "aims_launcher.py",
    _ROOT / "run_standalone.py",
    _ROOT / "build_exe.spec",
    _ROOT / "requirements-exe.txt",
    _ROOT / "build/build_common.ps1",
    *_ROOT.glob("installer/*.iss"),
]


def test_no_webview_in_code_build_or_installer():
    offenders = [
        str(p.relative_to(_ROOT))
        for p in _SCANNED
        if p.is_file() and _BANNED.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == []
