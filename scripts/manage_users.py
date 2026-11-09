#!/usr/bin/env python3
"""用户运维命令行入口(实现在 app/cli/manage_users.py, 这样打包的 EXE 里也带着它)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli.manage_users import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
