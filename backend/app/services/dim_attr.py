"""维度属性服务：管理维度表的属性字段定义、衍生字段逻辑和历史追踪策略。

登记依据：
- 维度属性发布快照是属性登记的唯一依据，按维度编码校验属性归属
- 只使用必填字段齐全的生效快照；迟到的可空属性留在待确认快照里等待切换
- 同名字段与主键字段类型都按快照核对，登记结果回写所依据的版本
- 快照切换完成前，原生效快照继续供应查询

增强能力：
- 派生字段计算：基于已有字段生成衍生属性值
- 历史追踪策略校验：确保SCD类型2关联的属性开启历史追踪
- 属性下线时通知下游模块
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

VALID_TYPES = {"VARCHAR", "INT", "BIGINT", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN", "TEXT", "JSON"}
DERIVED_PREFIX = "_derived_"


class DimAttrService(BaseModuleService):
    # 维度发布快照是本模块登记的发布依据（只登记到必填齐全的生效快照）
    MODULE = "dim_attr"
    ENTITY = "属性字段"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["属性名称", "所属维度", "数据类型", "字段来源", "是否派生", "历史追踪", "默认值"]
    REQUIRED_FIELDS = ["属性名称", "所属维度", "数据类型"]
    STATUS_ORDER = ["草稿", "已确认", "已下线"]
    ACTION_RULES = {"确认属性": "草稿", "下线属性": "已确认"}
    NEGATIVE_ACTIONS = ["下线属性"]

    # 本模块的动作会推进状态：确认 → 已确认，下线 → 已下线
    TRANSITIONS = {"确认属性": "已确认", "下线属性": "已下线"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建维度属性：按维度属性发布快照校验归属、同名字段和主键类型。"""
        # 标准化数据类型：保留长度/精度，只校正基础类型
        data_type = re.sub(r"\s+", "", str(values.get("数据类型", "VARCHAR"))).upper()
        base_type = data_type.split("(", 1)[0]
        if base_type not in VALID_TYPES:
            data_type = "VARCHAR"
        values["数据类型"] = data_type

        dim_code = str(values.get("所属维度", "")).strip()

        # 发布快照是唯一登记依据：没有生效快照时不能登记
        snapshot, errors, snapshot_fields = store.publish.validate_registration(values)
        if errors:
            return None, errors

        # 记录属性归属（以快照的维度编码为准）与所依据的版本
        values["属性归属"] = snapshot["dim_code"]

        # 检测是否派生字段
        source_field = str(values.get("字段来源", ""))
        if DERIVED_PREFIX in source_field:
            values["是否派生"] = "是"

        # 历史追踪策略校验：SCD类型2关联的维度属性必须开启历史追踪
        for dim in store.rows("dim_definition"):
            if dim.get("维度编码") == dim_code and "SCD类型2" in str(dim.get("SCD类型", "")):
                values["历史追踪"] = "是"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 保留可空/必填声明，快照据此判断发布必填字段
        for field in ("是否可空", "是否必填"):
            if field in values:
                entry[field] = values[field]

        # 回写登记依据：快照版本与生效区间跟随快照确认
        store.publish.bind_attribute(entry, snapshot)
        entry["快照状态"] = snapshot["status"]

        # 发布属性定义到数据通道
        store.channel.publish("attr.defined", {
            "attr_name": entry["属性名称"],
            "dim_code": dim_code,
            "data_type": data_type,
            "publish_snapshot_id": entry.get("发布快照ID"),
            "publish_version": entry.get("发布快照版本"),
            "is_derived": entry.get("是否派生") == "是",
            "history_tracking": entry.get("历史追踪") == "是",
            "source_field": source_field,
            "entry_id": entry["id"],
        })

        # 注册属性产物
        store.artifacts.register(
            "dim_attr", "dimension_attribute",
            f"attr_{entry['id']}",
            {
                "attr_name": entry["属性名称"],
                "dim_code": dim_code,
                "data_type": data_type,
                "publish_version": entry.get("发布快照版本"),
            },
            source_modules=["dim_definition", "dim_attr"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        # 推进到动作目标状态（确认/下线），状态流转只在这里发生
        target = self.TRANSITIONS.get(action)
        if target is not None:
            entry["status"] = target
            entry["pending"] = target != self.STATUS_ORDER[-1]
            entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        dim_code = entry.get("所属维度", "")
        if action == "确认属性":
            # 确认属性后，重置关联维度定义状态
            _validate_dim_attr_integrity(dim_code)

            # 属性确认后核对所属发布快照：必填字段补齐即可完成切换
            store.publish.on_attribute_confirmed(entry)
            snapshot = store.publish.snapshot_by_id(str(entry.get("发布快照ID") or ""))
            entry["快照状态"] = snapshot["status"] if snapshot else "待确认"

            # 通知SCD处理器属性已确认
            store.channel.publish("attr.confirmed", {
                "attr_name": entry["属性名称"],
                "dim_code": dim_code,
                "data_type": entry["数据类型"],
                "publish_snapshot_id": entry.get("发布快照ID"),
                "publish_version": entry.get("发布快照版本"),
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
                "publish_version": entry.get("发布快照版本"),
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

        # 登记所依据的发布快照版本
        versions: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            version = str(row.get("发布快照版本") or "").strip()
            if version:
                versions[version] = versions.get(version, 0) + 1
        base["registered_versions"] = versions
        base["publish_snapshots"] = store.publish.summary()
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
