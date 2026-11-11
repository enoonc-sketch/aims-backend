"""启动器后端看门狗策略测试（进程异常退出 / 重复启动 / 后台清理）。

需求（Windows 无 CMD 改造，第 10 条）：检查进程退出、异常退出、重复启动及
后台进程清理机制。这里的策略函数决定"后端崩溃后是自愈还是止损"，
必须是可预测的——崩溃-重启死循环比崩溃本身更难排查。
"""
from app.launcher.main import App


def test_restart_then_give_up_after_limit():
    """连续崩溃：前 3 次重启，第 4 次放弃（不再无限重启）。"""
    actions = [App._restart_policy(i, since_last_start=600.0)[0] for i in range(5)]
    assert actions == ["restart", "restart", "restart", "give_up", "give_up"]


def test_quick_crash_backs_off_exponentially():
    """刚启动就崩（<60s）：退避 1.5s → 3s → 6s，避免把 CPU 打满。"""
    delays = [App._restart_policy(i, since_last_start=5.0)[1] for i in range(3)]
    assert delays == [1.5, 3.0, 6.0]
    assert delays == sorted(delays)


def test_stable_run_restarts_promptly():
    """稳定运行后再崩（>=60s）：2 秒内重启，尽快恢复业务。"""
    assert App._restart_policy(0, since_last_start=3600.0) == ("restart", 2.0)


def test_give_up_has_no_delay():
    assert App._restart_policy(App.MAX_BACKEND_RESTARTS, 1.0) == ("give_up", 0.0)


def test_watchdog_only_manages_own_backend():
    """后端不是本启动器拉起的（例如系统服务/管理员手动启动）时不得接管。"""
    class _Sup:
        started_backend_here = False
        backend = None

    app = App.__new__(App)          # 不跑 __init__（避免建托盘/日志），只测守卫
    app.supervisor = _Sup()
    app._watchdog_started = False
    App._start_backend_watchdog(app)
    assert app._watchdog_started is False, "不该为别人的进程起看门狗"


def test_terminate_tree_uses_ctrl_break_before_kill():
    """退出顺序：先 CTRL_BREAK 走 uvicorn 正常关停，超时才 taskkill /T 收树。"""
    from app.launcher import supervisor as sup_mod

    src = (sup_mod.__file__)
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "CTRL_BREAK_EVENT" in text
    assert '"taskkill", "/PID", str(proc.pid), "/T", "/F"' in text
    # 窗口隐藏必须带上（否则关服务时闪黑框）
    assert 'getattr(subprocess, "CREATE_NO_WINDOW", 0)' in text
