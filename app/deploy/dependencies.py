"""组件依赖关系表（AIMS 1.0 阶段 3-5 补齐，总方案第八部分 8.4）。

静态声明各组件的依赖关系，供：
- 服务停止/重启前提示"将影响哪些组件"；
- doctor 输出依赖一致性；
- launcher 按依赖顺序启动（与 supervisor 的硬编码顺序互为对照）。

只描述 AIMS 自身管理的组件；外部依赖（如 Windows 服务）不在此表。
"""
from __future__ import annotations

from typing import Dict, List

# component -> 依赖的组件列表（启动顺序应保证依赖先就绪）
COMPONENT_DEPENDENCIES: Dict[str, List[str]] = {
    # 数据库是一切的底座
    "postgresql": [],
    # 后端需要数据库（业务数据 + pgvector 检索）
    "backend": ["postgresql"],
    # 桌面窗口只是后端 UI 的容器
    "frontend": ["backend"],
    # 本地 AI 推理在后端进程内运行，读取模型目录；不直接依赖数据库写路径，
    # 但模型注册表/激活状态存于数据库
    "ai-engine": ["postgresql", "backend"],
    # 外部数据抓取通过后端 API 写入数据库
    "external-scraper": ["backend", "postgresql"],
}

# 反向影响：stopping -> 会被影响的组件
def dependents_of(component: str) -> List[str]:
    """谁依赖 component（停止它会影响谁）。"""
    return sorted(
        name for name, deps in COMPONENT_DEPENDENCIES.items() if component in deps
    )


def impact_of_stopping(component: str) -> Dict[str, List[str]]:
    """停止 component 的影响报告：直接受影响组件 + 链式影响。"""
    direct = dependents_of(component)
    chain: List[str] = []
    frontier = list(direct)
    seen = set(direct)
    while frontier:
        cur = frontier.pop()
        for dep in dependents_of(cur):
            if dep not in seen:
                seen.add(dep)
                chain.append(dep)
                frontier.append(dep)
    return {"stopping": component, "direct": direct, "chain": sorted(chain)}


def validate_startup_order(order: List[str]) -> List[str]:
    """校验一个启动顺序是否满足依赖（返回违规列表；空列表=顺序合法）。"""
    ready: set[str] = set()
    violations: List[str] = []
    for name in order:
        deps = COMPONENT_DEPENDENCIES.get(name, [])
        missing = [d for d in deps if d not in ready]
        if missing:
            violations.append(f"{name} 启动时缺少依赖: {', '.join(missing)}")
        ready.add(name)
    return violations
