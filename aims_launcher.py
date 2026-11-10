"""PyInstaller 入口：构建 AIMS.exe（桌面启动器）。

开发时等价用法：
    python aims_launcher.py --no-browser
"""
from __future__ import annotations

import sys


def main() -> int:
    from app.launcher.main import main as launcher_main

    return launcher_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
