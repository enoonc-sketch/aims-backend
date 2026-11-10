@echo off
REM ============================================================================
REM  AIMS Backend — 轻量版 EXE 构建（报告第 8 项，2026-11-09）
REM  与 build_exe.bat 完全相同，只是跳过可选 AI 栈（llama / sklearn /
REM  statsmodels，约 240MB），打出的 EXE 约 149MB。轻量版里本地 LLM、
REM  异常检测、趋势预测等 AI 功能不可用，导入/诊断/规则模式照常用。
REM  轻量版已是默认；完整版请先 set AIMS_BUILD_AI=1 再运行 build_exe.bat。
REM ============================================================================
set "AIMS_BUILD_AI=0"
call "%~dp0build_exe.bat"
