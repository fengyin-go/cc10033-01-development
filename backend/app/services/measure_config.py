"""度量配置服务：管理事实表的度量字段定义、类型和聚合方式。

增强能力：
- 消费 fact.defined 通道，关联度量到对应事实表
- 自动推断度量类型和聚合方式（基于字段命名规则）
- 可加性校验：非可加度量使用COUNT DISTINCT聚合
- 消费 measure.suggested 自动创建建议度量
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

MEASURE_PATTERNS = {
    "_amt": ("DECIMAL(18,2)", "SUM", True),
    "_qty": ("BIGINT", "SUM", True),
    "_cnt": ("BIGINT", "COUNT", False),
    "_rate": ("DECIMAL(5,4)", "AVG", False),
    "_days": ("DECIMAL(10,2)", "AVG", False),
}


class MeasureConfigService(BaseModuleService):
    MODULE = "measure_config"
    ENTITY = "度量字段"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["度量名称", "所属事实", "度量类型", "聚合方式", "精度要求", "是否可加", "来源字段"]
    REQUIRED_FIELDS = ["度量名称", "所属事实", "度量类型"]
    STATUS_ORDER = ["草稿", "已确认", "已停用"]
    ACTION_RULES = {"确认度量": "草稿", "停用度量": "已确认"}
    NEGATIVE_ACTIONS = ["停用度量"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建度量配置：消费事实定义通道，智能推断度量属性和聚合方式。"""
        measure_name = str(values.get("度量名称", "")).strip()

        # 智能推断度量类型和聚合方式
        inferred = _infer_measure_props(measure_name)
        if not values.get("度量类型"):
            values["度量类型"] = inferred["type"]
        if not values.get("聚合方式"):
            values["聚合方式"] = inferred["agg_method"]
        if not values.get("是否可加"):
            values["是否可加"] = "是" if inferred["additive"] else "否"
        if not values.get("精度要求"):
            values["精度要求"] = inferred["precision"]

        # 消费事实定义通道
        fact_name = str(values.get("所属事实", "")).strip()
        fact_msgs = store.channel.consume("fact.defined", "measure_config")
        for msg in fact_msgs:
            if msg["payload"].get("fact_name") == fact_name:
                values["_fact_business_process"] = msg["payload"].get("business_process", "")
                values["_fact_granularity"] = msg["payload"].get("granularity", "")

        # 消费度量建议通道
        suggested = store.channel.consume("measure.suggested", "measure_config")
        for s in suggested:
            if s["payload"].get("fact_name") == fact_name:
                # 核对建议与当前输入
                values["度量类型"] = s["payload"].get("measure_type", values.get("度量类型", "DECIMAL(18,2)"))
                values["聚合方式"] = s["payload"].get("agg_method", values.get("聚合方式", "SUM"))
                values["是否可加"] = "是" if s["payload"].get("is_additive") else "否"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布度量配置到数据通道
        store.channel.publish("measure.confirmed", {
            "measure_name": entry["度量名称"],
            "fact_name": fact_name,
            "measure_type": entry["度量类型"],
            "agg_method": entry["聚合方式"],
            "is_additive": entry["是否可加"] == "是",
            "source_field": entry.get("来源字段", measure_name),
            "entry_id": entry["id"],
        })

        # 注册度量产物
        store.artifacts.register(
            "measure_config", "measure_definition",
            f"measure_{entry['id']}",
            {"measure_name": entry["度量名称"], "fact_name": fact_name, "agg_method": entry["聚合方式"]},
            source_modules=["fact_design", "measure_config"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "确认度量":
            # 可加性校验
            is_additive = entry.get("是否可加") == "是"
            agg_method = entry.get("聚合方式", "SUM")
            if not is_additive and agg_method == "SUM":
                # 非可加度量不能使用SUM
                entry["聚合方式"] = "COUNT DISTINCT"
                entry["pending"] = True

            store.channel.publish("measure.ready", {
                "measure_name": entry["度量名称"],
                "fact_name": entry.get("所属事实", ""),
                "agg_method": entry["聚合方式"],
                "is_additive": is_additive,
                "entry_id": entry["id"],
            })
        elif action == "停用度量":
            store.channel.publish("measure.decommissioned", {
                "measure_name": entry["度量名称"],
                "entry_id": entry["id"],
            })

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 度量类型分布
        type_dist: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            t = row.get("度量类型", "未指定")
            type_dist[t] = type_dist.get(t, 0) + 1
        base["measure_type_distribution"] = type_dist

        # 可加性统计
        additive = sum(1 for row in store.rows(self.MODULE) if row.get("是否可加") == "是")
        base["additive_measures"] = additive
        base["unadditive_measures"] = len(store.rows(self.MODULE)) - additive

        # 消费统计
        base["consumed_facts"] = len(store.channel.consume("fact.defined", "measure_config"))
        return base


def _infer_measure_props(measure_name: str) -> dict[str, Any]:
    """基于度量字段命名推断属性。"""
    for suffix, (m_type, agg, additive) in MEASURE_PATTERNS.items():
        if measure_name.endswith(suffix):
            return {
                "type": m_type,
                "agg_method": agg,
                "additive": additive,
                "precision": "两位小数" if "DECIMAL" in m_type else "整数",
            }
    return {"type": "BIGINT", "agg_method": "SUM", "additive": True, "precision": "整数"}