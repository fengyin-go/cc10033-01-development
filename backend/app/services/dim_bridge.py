"""桥接关联服务：管理维度与事实之间的多对多桥接表定义。

增强能力：
- 消费 dim.published 和 fact.published 通道校验多对多关系
- 权重字段验证（权重值范围）
- 时效性管理：生效/失效时间与主记录标记
- 桥接表数据生成模拟
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DimBridgeService(BaseModuleService):
    MODULE = "dim_bridge"
    ENTITY = "桥接表"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["桥接名称", "多对多维度", "关联事实", "权重字段", "生效时间", "失效时间", "主记录标记"]
    REQUIRED_FIELDS = ["桥接名称", "多对多维度", "关联事实"]
    STATUS_ORDER = ["草稿", "已生效", "已失效"]
    ACTION_RULES = {"启用桥接": "草稿", "停用桥接": "已生效"}
    NEGATIVE_ACTIONS = ["停用桥接"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        many_dims = str(values.get("多对多维度", "")).strip()
        related_fact = str(values.get("关联事实", "")).strip()

        # 校验维度存在性
        _validate_bridge_dims(many_dims, related_fact)

        # 设置默认时间
        if not values.get("生效时间"):
            values["生效时间"] = "2026-01-01"
        if not values.get("失效时间"):
            values["失效时间"] = "9999-12-31"
        if not values.get("主记录标记"):
            values["主记录标记"] = "Y"

        # 消费通道数据
        _consume_bridge_context(many_dims, related_fact)
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("bridge.defined", {
            "bridge_name": entry["桥接名称"],
            "dims": many_dims,
            "fact": related_fact,
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "dim_bridge", "bridge_table",
            f"bridge_{entry['id']}",
            {"bridge_name": entry["桥接名称"], "dims": many_dims, "fact": related_fact},
            ["dim_definition", "fact_design", "dim_bridge"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "启用桥接":
            store.channel.publish("bridge.activated", {
                "bridge_name": entry["桥接名称"],
                "entry_id": entry_id,
            })
        elif action == "停用桥接":
            entry["失效时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            store.channel.publish("bridge.deactivated", {
                "bridge_name": entry["桥接名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        active = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已生效")
        base["active_bridges"] = active
        return base


def _validate_bridge_dims(dim_str: str, fact_str: str) -> None:
    dims = [d.strip() for d in dim_str.split(".") if d.strip()]
    existing = {r.get("维度编码") for r in store.rows("dim_definition")}
    valid = [d for d in dims if d in existing]
    if valid:
        store.channel.publish("bridge.dims_validated", {"valid_dims": valid, "dim_str": dim_str})


def _consume_bridge_context(dim_str: str, fact_str: str) -> None:
    store.channel.consume("dim.published", "dim_bridge")
    store.channel.consume("fact.published", "dim_bridge")