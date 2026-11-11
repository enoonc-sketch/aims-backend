"""阶段 3-5 补齐：组件依赖关系表（总方案 8.4）。"""
from app.deploy.dependencies import (
    COMPONENT_DEPENDENCIES,
    dependents_of,
    impact_of_stopping,
    validate_startup_order,
)


def test_postgresql_is_base_dependency():
    assert COMPONENT_DEPENDENCIES["postgresql"] == []
    deps = dependents_of("postgresql")
    assert "backend" in deps


def test_impact_of_stopping_postgresql_is_transitive():
    impact = impact_of_stopping("postgresql")
    assert "backend" in impact["direct"]
    assert "ai-engine" in impact["direct"]  # 直接依赖数据库
    assert "frontend" in impact["chain"]    # 仅通过 backend 间接受影响


def test_supervisor_style_startup_order_is_valid():
    # supervisor 实际顺序：数据库 → AI → 后端 → 界面
    assert validate_startup_order(["postgresql", "backend", "ai-engine", "frontend"]) == []


def test_bad_order_is_flagged():
    v = validate_startup_order(["frontend", "postgresql"])
    assert v and "frontend" in v[0]
