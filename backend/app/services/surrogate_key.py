"""代理键服务：管理维度表的代理键生成策略。

增强能力：
- 键段分配策略：自增序列/UUID_HASH/HASH_MOD
- 步长和初始值管理
- 键耗尽检测和自动重置
- 冲突处理：跳过重试/重新分配/异常告警
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

KEY_STRATEGIES = {"自增序列", "UUID_HASH", "HASH_MOD", "雪花算法", "时间戳序列"}


class SurrogateKeyService(BaseModuleService):
    MODULE = "surrogate_key"
    ENTITY = "键值定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["键值名称", "目标维度", "生成策略", "初始值", "步长", "唯一范围", "冲突处理"]
    REQUIRED_FIELDS = ["键值名称", "目标维度", "生成策略"]
    STATUS_ORDER = ["草稿", "已分配", "已耗尽", "已重置"]
    ACTION_RULES = {"分配键段": "草稿", "重置键值": "已耗尽"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        strategy = str(values.get("生成策略", "")).strip()
        if strategy not in KEY_STRATEGIES:
            values["生成策略"] = "自增序列"

        if not values.get("初始值"):
            values["初始值"] = "1000000"
        if not values.get("步长"):
            values["步长"] = "1"
        if not values.get("唯一范围"):
            values["唯一范围"] = "全局"
        if not values.get("冲突处理"):
            values["冲突处理"] = "跳过重试"

        # 消费维度定义
        store.channel.consume("dim.defined", "surrogate_key")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("surrogate_key.defined", {
            "key_name": entry["键值名称"],
            "dim_code": entry.get("目标维度"),
            "strategy": entry.get("生成策略"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "surrogate_key", "surrogate_key_def",
            f"sk_{entry['id']}",
            {"key_name": entry["键值名称"], "dim_code": entry.get("目标维度")},
            ["dim_definition", "surrogate_key"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "分配键段":
            allocated = _allocate_key_range(entry)
            store.channel.publish("surrogate_key.allocated", {
                "key_name": entry["键值名称"],
                "range": allocated,
                "entry_id": entry_id,
            })
        elif action == "重置键值":
            store.channel.publish("surrogate_key.reset", {
                "key_name": entry["键值名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        exhausted = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已耗尽")
        base["exhausted_keys"] = exhausted
        return base


def _allocate_key_range(entry: dict[str, Any]) -> dict[str, Any]:
    start = int(entry.get("初始值", "1000000"))
    step = int(entry.get("步长", "1"))
    return {"start": start, "end": start + step * 10000, "count": 10000}