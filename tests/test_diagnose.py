"""启动自诊断报告测试。

现场只有"装完打不开"一句话时，维护者需要一份能直接定性的材料：路径权限、
生效配置、数据库状态、端口占用、日志尾部。这里锁定三件事：

1. **不泄密**：口令一律脱敏（DATABASE_URL 密码段、含 PASSWORD/SECRET/TOKEN 的值）；
2. **不崩**：诊断代码自己失败不能把主流程带崩（布局残缺也要能出报告）；
3. **可读**：报告包含各关键分节，且失败原因写在最前面。
"""
from __future__ import annotations

from pathlib import Path

from app.deploy.diagnose import (
    _pad,
    collect,
    env_snapshot,
    mask_url,
    render,
    write_report,
)


def test_mask_url_hides_password():
    url = "postgresql://aims:S3cr3t-Pa55@127.0.0.1:55432/aims"
    masked = mask_url(url)
    assert "S3cr3t-Pa55" not in masked
    assert masked.startswith("postgresql://aims:***@")
    assert masked.endswith("/aims")
    # 无密码的连接串原样返回（不要凭空加 ***）
    assert mask_url("sqlite:///C:/data/aims.db") == "sqlite:///C:/data/aims.db"
    assert mask_url("") == ""


def test_env_snapshot_masks_secret_values(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://aims:pw123456@h:5432/db")
    monkeypatch.setenv("AIMS_PG_PORT", "55432")
    snap = env_snapshot()
    assert "pw123456" not in str(snap)
    assert snap["AIMS_PG_PORT"] == "55432"      # 非敏感项照常显示
    # 白名单之外的变量（含敏感词）不会出现在报告里
    monkeypatch.setenv("SOME_OTHER_TOKEN", "abcdef")
    assert "SOME_OTHER_TOKEN" not in env_snapshot()


def test_collect_and_render_has_sections(tmp_path, monkeypatch):
    layout = tmp_path / "root"
    data = tmp_path / "data"
    (data / "Config").mkdir(parents=True)
    (data / "Logs").mkdir(parents=True)
    # 产品形态：程序目录 + 数据根分离（AIMS_DATA_ROOT 决定 Config/Logs 落在哪）
    monkeypatch.setenv("AIMS_DATA_ROOT", str(data))
    (data / "Config" / "aims.env").write_text(
        "DATABASE_URL=postgresql://aims:topsecret@127.0.0.1:55432/aims\n"
        "AIMS_PG_PORT=55432\n",
        encoding="utf-8",
    )
    (data / "Logs" / "pgsql-startup.log").write_text(
        "FATAL: could not create shared memory segment\n", encoding="utf-8"
    )

    report = collect(layout, reason="数据库启动失败：测试")
    text = render(report)

    for section in ("运行环境", "路径与权限", "生效配置", "数据库", "日志尾部", "失败现象"):
        assert section in text, f"报告缺少分节：{section}"
    assert "数据库启动失败" in text
    assert "could not create shared memory" in text, "必须带日志尾部（真正的原因常在这里）"
    assert "topsecret" not in text, "报告里绝不能出现明文口令"
    assert "aims:***@" in text


def test_write_report_creates_file_and_latest_copy(tmp_path, monkeypatch):
    layout = tmp_path / "root"
    data = tmp_path / "data"
    (data / "Logs").mkdir(parents=True)
    monkeypatch.setenv("AIMS_DATA_ROOT", str(data))
    path = write_report(layout, reason="端口 8000 被占用")
    assert path is not None and path.is_file()
    body = path.read_text(encoding="utf-8")
    assert "端口 8000 被占用" in body
    latest = path.parent / "aims-diagnose-latest.txt"
    assert latest.is_file(), "要有一个固定文件名的副本，用户/客服只认一个名字"


def test_write_report_survives_broken_layout(tmp_path):
    """布局残缺（目录不存在、无配置）时也必须出报告，而不是抛异常。"""
    path = write_report(tmp_path / "does-not-exist" / "root", reason="测试")
    assert path is not None
    assert path.is_file()


def test_pad_aligns_cjk_display_width():
    """中文按显示宽度补齐：'程序'(4 列) 与 'abcdef'(6 列) 补到同一宽度后等宽。"""
    assert len(_pad("程序", 14)) != len(_pad("abcdef", 14))  # 字符数不同
    assert _width_of(_pad("程序", 14)) == _width_of(_pad("abcdef", 14)) == 14


def _width_of(text: str) -> int:
    from app.deploy.diagnose import _width

    return _width(text)


# --------------------------------------------------------------------------- #
# 安装配置必须自描述（现场"装完打不开"的一个真实成因）
#
# provision 可能用了非默认数据目录（D 盘、独立数据盘、迁移后的目录）。这些路径
# 如果只存在于当时的环境变量里，重启后没人知道集群在哪，程序就会去默认位置找
# PG_VERSION，然后报"数据目录不存在"——现象与真实原因完全对不上。
# --------------------------------------------------------------------------- #
def test_build_env_persists_cluster_and_data_paths(tmp_path):
    from app.deploy.conf import build_env, render_env
    from app.deploy.paths import Layout

    layout = Layout(root=tmp_path / "root", data_root=tmp_path / "data")
    values = build_env(
        layout,
        {"pg": {"host": "127.0.0.1", "app_user": "aims", "database": "aims"}},
        {"DB_PASSWORD": "x", "SECRET_KEY": "y", "ADMIN_PASSWORD": "z",
         "BOOTSTRAP_PASSWORD": "w"},
        port=55432, api_port=8000,
    )
    assert values["AIMS_PG_DATA"] == str(layout.pg_data), "集群位置必须落盘"
    assert values["AIMS_PG_BINDIR"] == str(layout.pg_bin)
    assert values["AIMS_DATA_ROOT"] == str(layout.base), "数据根必须落盘"
    text = render_env(values)
    for key in ("AIMS_PG_DATA=", "AIMS_PG_BINDIR=", "AIMS_DATA_ROOT="):
        assert key in text


def test_verdicts_explain_port_taken_by_foreign_cluster(tmp_path, monkeypatch):
    """端口被占 + 没有 postmaster.pid → 必须明说"占用者不是 AIMS 的集群"。"""
    from app.deploy.diagnose import verdicts

    report = {
        "runtime": {"api_port": 8000, "api_port_state": "空闲"},
        "database": {
            "pg_ctl_exists": True, "initialized": False,
            "data_dir": str(tmp_path / "Database"),
            "port_state": "已占用（有服务在监听）", "postmaster_pid": None,
            "pg_port_effective": 55432,
        },
        "paths": {"明细": {}},
        "logs": {},
    }
    joined = " ".join(verdicts(report))
    assert "未初始化" in joined and "AIMS_PG_DATA" in joined
    assert "55432" in joined and "不是 AIMS 自己的集群" in joined


def test_verdicts_empty_when_install_healthy(tmp_path):
    from app.deploy.diagnose import verdicts

    report = {
        "runtime": {"api_port": 8000, "api_port_state": "空闲"},
        "database": {"pg_ctl_exists": True, "initialized": True,
                     "postmaster_pid": 1234, "ready": True,
                     "port_state": "已占用（有服务在监听）", "pg_port_effective": 55432},
        "paths": {"明细": {
            "配置目录": {"存在": True, "可写": "可写", "路径": "/x/Config"},
            "日志目录": {"存在": True, "可写": "可写", "路径": "/x/Logs"},
            "数据根": {"存在": True, "可写": "可写", "路径": "/x"},
        }},
        "logs": {"aims.log": "ok"},
    }
    assert verdicts(report) == [], "健康的安装不该出现任何告警，否则告警会被忽略"


def test_bootstrap_applies_config_paths_before_managed_check(tmp_path, monkeypatch):
    """配置里的集群路径必须先生效，「是不是托管安装」才判得准。

    否则自定义数据目录的安装会被判成"非托管"，跳过整个数据库引导，
    表现为装完打不开（或更糟：以空 SQLite 库启动，历史数据还在原来的库里）。
    """
    from app.deploy import runtime
    from app.deploy.paths import load_layout

    root = tmp_path / "root"
    data = tmp_path / "data"
    custom_pg = tmp_path / "pgdata-custom"
    (data / "Config").mkdir(parents=True)
    (data / "Config" / "aims.env").write_text(
        f"AIMS_PG_DATA={custom_pg}\n"
        "DATABASE_URL=postgresql+psycopg2://aims:x@127.0.0.1:55432/aims\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIMS_DATA_ROOT", str(data))
    monkeypatch.delenv("AIMS_PG_DATA", raising=False)

    layout = load_layout(root)
    assert layout.pg_data != custom_pg  # 应用前：默认位置

    env = runtime._apply_config_paths(layout)
    assert env["AIMS_PG_DATA"] == str(custom_pg)
    import os

    assert os.environ["AIMS_PG_DATA"] == str(custom_pg)
    assert load_layout(root).pg_data == custom_pg  # 应用后：按配置来


def test_config_paths_do_not_override_real_environment(tmp_path, monkeypatch):
    """支持工程师临时把一次运行指向另一套库时，真实环境变量必须赢。"""
    from app.deploy import runtime
    from app.deploy.paths import load_layout

    data = tmp_path / "data"
    (data / "Config").mkdir(parents=True)
    (data / "Config" / "aims.env").write_text("AIMS_PG_DATA=/from/config\n", encoding="utf-8")
    monkeypatch.setenv("AIMS_DATA_ROOT", str(data))
    monkeypatch.setenv("AIMS_PG_DATA", "/from/env")

    runtime._apply_config_paths(load_layout(tmp_path / "root"))
    import os

    assert os.environ["AIMS_PG_DATA"] == "/from/env"
