"""维度属性服务：管理维度表的属性字段定义、衍生字段逻辑和历史追踪策略。

增强能力：
- 消费 dim.defined 通道消息，关联属性到对应维度
- 派生字段计算：基于已有字段生成衍生属性值
- 历史追踪策略校验：确保SCD类型2关联的属性开启历史追踪
- 属性下线时通知下游模块
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

VALID_TYPES = {"VARCHAR", "INT", "BIGINT", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN", "TEXT", "JSON"}
DERIVED_PREFIX = "_derived_"


class DimAttrService(BaseModuleService):
    MODULE = "dim_attr"
    ENTITY = "属性字段"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["属性名称", "所属维度", "数据类型", "字段来源", "是否派生", "历史追踪", "默认值"]
    REQUIRED_FIELDS = ["属性名称", "所属维度", "数据类型"]
    STATUS_ORDER = ["草稿", "已确认", "已下线"]
    ACTION_RULES = {"确认属性": "草稿", "下线属性": "已确认"}
    NEGATIVE_ACTIONS = ["下线属性"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建维度属性：消费 dim.defined 通道，关联维度定义，校验数据类型。"""
        # 标准化数据类型
        data_type = str(values.get("数据类型", "VARCHAR")).strip().upper()
        if data_type not in VALID_TYPES:
            data_type = "VARCHAR"
            values["数据类型"] = data_type

        # 消费维度定义通道，获取维度信息
        dim_code = str(values.get("所属维度", "")).strip()
        dim_msgs = store.channel.consume_latest("dim.defined", "dim_attr")
        if dim_msgs and dim_msgs["payload"].get("dim_code") == dim_code:
            # 维度已定义，关联属性
            values["维度关联"] = dim_msgs["payload"]["dim_name"]

        # 检测是否派生字段
        source_field = str(values.get("字段来源", ""))
        if DERIVED_PREFIX in source_field:
            values["是否派生"] = "是"

        # 历史追踪策略校验：SCD类型2关联的维度属性必须开启历史追踪
        dim_rows = store.rows("dim_definition")
        for dim in dim_rows:
            if dim.get("维度编码") == dim_code and "SCD类型2" in str(dim.get("SCD类型", "")):
                values["历史追踪"] = "是"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布属性定义到数据通道
        store.channel.publish("attr.defined", {
            "attr_name": entry["属性名称"],
            "dim_code": dim_code,
            "data_type": data_type,
            "is_derived": entry.get("是否派生") == "是",
            "history_tracking": entry.get("历史追踪") == "是",
            "source_field": source_field,
            "entry_id": entry["id"],
        })

        # 注册属性产物
        store.artifacts.register(
            "dim_attr", "dimension_attribute",
            f"attr_{entry['id']}",
            {"attr_name": entry["属性名称"], "dim_code": dim_code, "data_type": data_type},
            source_modules=["dim_definition", "dim_attr"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        dim_code = entry.get("所属维度", "")
        if action == "确认属性":
            # 确认属性后，重置关联维度定义状态
            _validate_dim_attr_integrity(dim_code)

            # 通知SCD处理器属性已确认
            store.channel.publish("attr.confirmed", {
                "attr_name": entry["属性名称"],
                "dim_code": dim_code,
                "data_type": entry["数据类型"],
                "is_derived": entry.get("是否派生") == "是",
                "history_tracking": entry.get("历史追踪") == "是",
                "default_value": entry.get("默认值"),
                "entry_id": entry["id"],
            })
        elif action == "下线属性":
            # 通知下游模块属性已下线
            store.channel.publish("attr.decommissioned", {
                "attr_name": entry["属性名称"],
                "dim_code": dim_code,
                "entry_id": entry["id"],
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 按维度聚合属性
        attr_by_dim: dict[str, list[str]] = {}
        for row in store.rows(self.MODULE):
            dim = row.get("所属维度", "未关联")
            attr_by_dim.setdefault(dim, []).append(row.get("属性名称", ""))
        base["attributes_by_dimension"] = attr_by_dim

        # 获取维度定义消费统计
        consumed_dims = store.channel.consume("dim.defined", "dim_attr")
        base["consumed_dim_definitions"] = len(consumed_dims)

        # 派生字段统计
        derived_count = sum(1 for row in store.rows(self.MODULE) if row.get("是否派生") == "是")
        base["derived_attributes"] = derived_count
        return base


def _validate_dim_attr_integrity(dim_code: str) -> None:
    """校验维度属性完整性，确保SCD类型2关联的属性全部开启历史追踪。"""
    dim_rows = store.rows("dim_definition")
    dim_scd = None
    for d in dim_rows:
        if d.get("维度编码") == dim_code:
            dim_scd = d.get("SCD类型", "")
            break

    attr_rows = store.rows("dim_attr")
    for attr in attr_rows:
        if attr.get("所属维度") == dim_code:
            if "SCD类型2" in str(dim_scd or ""):
                attr["历史追踪"] = "是"
            elif "SCD类型0" in str(dim_scd or "") or "SCD类型1" in str(dim_scd or ""):
                attr["历史追踪"] = "否"

    if dim_scd:
        store.channel.publish("attr.integrity_check", {
            "dim_code": dim_code,
            "scd_type": dim_scd,
            "checked_attrs": sum(1 for a in attr_rows if a.get("所属维度") == dim_code),
        })