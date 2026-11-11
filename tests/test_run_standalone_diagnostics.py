"""GUI 子系统 EXE 启动失败时的排障信息（1.0.0 现场缺陷回归）。

现象：安装后双击 EXE 只弹「Press Enter to exit...」，真正的失败原因
（配置错误 / 端口占用 / 崩溃堆栈）都 print 到了看不见的控制台里，
用户拿到的信息量为零。

修复：_capture_output 从进程启动就把 stdout/stderr 复制进内存缓冲，
_pause 的 MessageBox 正文带上这些内容。以下测试锁定：
  1. print / stderr 写入都能被捕获；
  2. sys.stdout 为 None（GUI EXE 的常态）时捕获与 print 都不崩；
  3. 弹窗正文 = 捕获输出 + 提示语，无输出时退回纯提示语；
  4. 捕获不吞输出——原流（调试控制台 / cmd 管道）照常收到内容。
"""
from __future__ import annotations

import io

import run_standalone as rs


def _restore_streams(saved):
    rs._output_buffer.clear()
    __import__("sys").stdout, __import__("sys").stderr = saved


def test_capture_output_collects_print_and_stderr():
    import sys

    saved = (sys.stdout, sys.stderr)
    try:
        rs._output_buffer.clear()
        rs._capture_output()
        print("hello-aims-startup")
        sys.stderr.write("CONFIGURATION ERROR — refusing to start\n")
        tail = rs._output_tail()
        assert "hello-aims-startup" in tail
        assert "CONFIGURATION ERROR" in tail
    finally:
        _restore_streams(saved)


def test_capture_with_none_stream_does_not_crash():
    """GUI 子系统 EXE：stdout/stderr 可能是 None，print 也不许崩。"""
    import sys

    saved = (sys.stdout, sys.stderr)
    try:
        sys.stdout = None
        sys.stderr = None
        rs._output_buffer.clear()
        rs._capture_output()
        print("captured-even-without-console")
        assert "captured-even-without-console" in rs._output_tail()
    finally:
        _restore_streams(saved)


def test_capture_is_idempotent():
    """调试模式重接控制台后再调用一次，不允许套两层（否则输出记两遍）。"""
    import sys

    saved = (sys.stdout, sys.stderr)
    try:
        rs._output_buffer.clear()
        rs._capture_output()
        first = sys.stdout
        assert isinstance(first, rs._Tee)
        rs._capture_output()
        assert sys.stdout is first, "重复捕获不得再包一层"
    finally:
        _restore_streams(saved)


def test_pause_dialog_text_includes_real_reason():
    rs._output_buffer.clear()
    rs._output_buffer.append(
        "  CONFIGURATION ERROR — refusing to start\n"
        "  SECRET_KEY is not set. It must be injected externally ...\n"
    )
    text = rs._pause_dialog_text("Press Enter to exit...")
    assert "SECRET_KEY is not set" in text, "弹窗必须带上真实原因"
    assert text.endswith("Press Enter to exit...")

    # 无输出时保持原提示（不要弹一个空壳窗）
    rs._output_buffer.clear()
    assert rs._pause_dialog_text("Press Enter to exit...") == "Press Enter to exit..."


def test_output_tail_keeps_latest_within_limit():
    rs._output_buffer.clear()
    for i in range(500):
        rs._output_buffer.append(f"line-{i}\n")
    tail = rs._output_tail(limit=200)
    assert len(tail) <= 200
    assert "line-499" in tail, "必须保留最新的输出"
    assert "line-0\n" not in tail, "最早的输出应被裁掉"


def test_capture_does_not_swallow_original_stream():
    """捕获是复制不是劫持：写到原流的内容必须原样到达（调试控制台靠它看日志）。"""
    import sys

    saved = (sys.stdout, sys.stderr)
    try:
        sink = io.StringIO()
        sys.stdout = sink
        rs._output_buffer.clear()
        rs._capture_output()
        print("both-places")
        assert "both-places" in sink.getvalue(), "原流必须照常收到输出"
    finally:
        _restore_streams(saved)


def test_thread_hook_reports_server_thread_exit(capsys, monkeypatch):
    """uvicorn 启动失败会在线程里 sys.exit(1)：这条必须留下输出。"""
    import threading
    from types import SimpleNamespace

    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    rs._install_thread_hook()
    threading.excepthook(SimpleNamespace(exc_type=SystemExit, exc_value=1, exc_traceback=None))
    out = capsys.readouterr().out
    assert "SystemExit" in out and "服务线程退出" in out


def test_thread_hook_prints_traceback_for_real_exception(capsys, monkeypatch):
    import threading
    from types import SimpleNamespace

    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    rs._install_thread_hook()
    try:
        raise RuntimeError("lifespan boom")
    except RuntimeError:
        import sys

        exc_type, exc_value, exc_tb = sys.exc_info()
        threading.excepthook(SimpleNamespace(exc_type=exc_type, exc_value=exc_value,
                                            exc_traceback=exc_tb))
    captured = capsys.readouterr()
    combined = captured.out + captured.err   # 堆栈走 stderr，提示语走 stdout
    assert "服务线程异常" in combined
    assert "lifespan boom" in combined
    assert "Traceback" in combined
