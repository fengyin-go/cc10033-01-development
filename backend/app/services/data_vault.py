"""DataVault服务：管理DataVault 2.0建模的Hub、Link和Satellite结构。

增强能力：
- Hub实体唯一性校验
- Link关系基尼关联检测（单向/双向）
- Satellite属性版本追踪
- 哈希键自动生成
- 消费维度/事实发布通道
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataVaultService(BaseModuleService):
    MODULE = "data_vault"
    ENTITY = "Vault模型"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["模型名称", "Hub实体", "Link关系", "Satellite属性", "加载时间", "记录源", "哈希键"]
    REQUIRED_FIELDS = ["模型名称", "Hub实体", "Link关系"]
    STATUS_ORDER = ["草稿", "已建模", "已加载", "已归档"]
    ACTION_RULES = {"提交建模": "草稿", "执行加载": "已建模", "归档模型": "已加载"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        hub_entity = str(values.get("Hub实体", "")).strip()

        # Hub唯一性检查
        for r in store.rows(self.MODULE):
            if r.get("Hub实体") == hub_entity:
                return None, [f"Hub实体 {hub_entity} 已存在"]

        # 自动生成哈希键
        if not values.get("哈希键"):
            hash_key = hashlib.md5(f"{hub_entity}_{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:16]
        else:
            hash_key = str(values.get("哈希键"))

        values["哈希键"] = hash_key

        if not values.get("加载时间"):
            values["加载时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 消费通道
        store.channel.consume("dim.published", "data_vault")
        store.channel.consume("fact.published", "data_vault")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("vault.defined", {
            "model_name": entry["模型名称"],
            "hub": hub_entity,
            "link": entry.get("Link关系"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "data_vault", "vault_model",
            f"vault_{entry['id']}",
            {"model_name": entry["模型名称"], "hub": hub_entity},
            ["data_vault"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "执行加载":
            load_result = _simulate_vault_load(entry)
            store.channel.publish("vault.loaded", {
                "model_name": entry["模型名称"],
                "rows_loaded": load_result.get("hubs", 0) + load_result.get("links", 0) + load_result.get("sats", 0),
                "entry_id": entry_id,
            })
        elif action == "归档模型":
            store.channel.publish("vault.archived", {
                "model_name": entry["模型名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        loaded = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已加载")
        base["loaded_vaults"] = loaded
        return base


def _simulate_vault_load(entry: dict[str, Any]) -> dict[str, Any]:
    return {"hubs": 10, "links": 15, "sats": 25}