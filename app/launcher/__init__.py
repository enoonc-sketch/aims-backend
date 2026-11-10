"""AIMS 桌面启动器（Windows 独立安装版的入口程序）。

运行形态：
    AIMS.exe                 —— 正常启动（托盘 + 自动打开主界面）
    AIMS.exe --no-browser    —— 只启动服务，不开界面（服务器/远程使用）
    AIMS.exe --headless      —— 无托盘，前台运行，Ctrl+C 退出（排障用）
    AIMS.exe --diagnose      —— 打印诊断报告后退出
    AIMS.exe --version
"""
from __future__ import annotations

APP_NAME = "AIMS"
PRODUCT_NAME = "AIMS 设备智能运维系统"
ENTRY_TITLE = "AIMS 桌面启动器"
