"""一致性维度服务：管理跨业务过程的一致性维度标准定义。

增强能力：
- 消费 dim.published 通道校验一致性
- 标准键值冲突检测：同主题下唯一
- 版本号管理：递增版本号
- 适用范围标准化
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

SCOPES = {"全仓", "零售域", "供应链域", "财务域", "营销域", "HR域"}


class ConformDimService(BaseModuleService):
    MODULE = "conform_dim"
    ENTITY = "标准维度"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["标准名称", "维度主题", "标准键值", "标准属性", "适用范围", "版本号", "发布状态"]
    REQUIRED_FIELDS = ["标准名称", "维度主题", "标准键值"]
    STATUS_ORDER = ["草稿", "已发布", "已废止"]
    ACTION_RULES = {"发布标准": "草稿", "废止标准": "已发布"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        scope = str(values.get("适用范围", "")).strip()
        if scope not in SCOPES:
            scope = "全仓"
            values["适用范围"] = scope

        # 标准键值冲突检测
        std_key = str(values.get("标准键值", "")).strip()
        for r in store.rows(self.MODULE):
            if r.get("标准键值") == std_key and r.get("维度主题") == values.get("维度主题"):
                if r.get("status") in ("草稿", "已发布"):
                    return None, [f"标准键值 {std_key} 在主题 {values.get('维度主题')} 下已存在"]

        if not values.get("版本号"):
            values["版本号"] = "v1.0"

        # 消费维度发布
        store.channel.consume("dim.published", "conform_dim")
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("conform_dim.defined", {
            "std_name": entry["标准名称"],
            "theme": entry["维度主题"],
            "std_key": entry["标准键值"],
            "scope": scope,
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "conform_dim", "conform_dim_standard",
            f"conform_{entry['id']}",
            {"std_name": entry["标准名称"], "theme": entry["维度主题"], "std_key": entry["标准键值"]},
            ["dim_definition", "conform_dim"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "发布标准":
            entry["发布状态"] = "已发布"
            store.channel.publish("conform_dim.published", {
                "std_name": entry["标准名称"],
                "entry_id": entry_id,
            })
        elif action == "废止标准":
            entry["发布状态"] = "已废止"
            store.channel.publish("conform_dim.deprecated", {
                "std_name": entry["标准名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        published = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已发布")
        base["published_standards"] = published
        return base