"""轻量读数对象：批量导入时替代 ORM 实例，避免为每一行付声明式构造器的代价。

为什么需要它：`Measurement(**kwargs)` 每创建一个实例都要走 SQLAlchemy 的
instrumentation（6 个字段就是 6 次属性事件），60k 行实测 1.05 s，占整个导入
耗时的四分之一；而这批对象从头到尾只是"待写入的数据"，不进 session、不参与
关系加载、不做脏检查。

因此批量导入改用它：同样的属性名（time/device_id/component_name/value/quality/
import_id），`__slots__` 避免 `__dict__`，构造走位置参数。数据链的质量评估、
报警判定、批量 upsert 只读这些属性，所以可以直接替换。

**单条/实时路径仍然用 ORM**：那条路径要把对象返回给 API（response_model）、
也可能被调用方 `db.add()`，换掉会改变语义。
"""
from __future__ import annotations

from datetime import datetime


class Reading:
    """与 `Measurement` 字段兼容的最小读数载体（仅用于批量写入）。"""

    __slots__ = ("time", "device_id", "component_name", "value", "quality", "import_id")

    def __init__(
        self,
        time: datetime,
        device_id: int,
        component_name: str,
        value: float,
        quality: str = "good",
        import_id: int | None = None,
    ) -> None:
        self.time = time
        self.device_id = device_id
        self.component_name = component_name
        self.value = value
        self.quality = quality
        self.import_id = import_id

    def __repr__(self) -> str:  # 排障时日志里能看懂
        return (f"Reading(time={self.time!r}, device_id={self.device_id!r}, "
                f"component_name={self.component_name!r}, value={self.value!r}, "
                f"quality={self.quality!r})")
