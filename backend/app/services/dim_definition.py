"""维度定义服务：管理维度表定义、主键设计、SCD类型配置。

增强能力：
- 创建维度时通过 DataChannel 发布维度定义，供 dim_attr 和 scd_handler 消费
- 维度编码重复校验与标准化转换
- SCD类型验证与默认值设定
- 发布维度时触发下游模块同步处理
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DimDefinitionService(BaseModuleService):
    MODULE = "dim_definition"
    ENTITY = "维度表"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["维度编码", "维度名称", "主键定义", "SCD类型", "粒度说明", "数据来源", "刷新频率"]
    REQUIRED_FIELDS = ["维度编码", "维度名称", "主键定义"]
    STATUS_ORDER = ["草稿", "已建模", "已发布", "已停用"]
    ACTION_RULES = {"提交建模": "草稿", "发布维度": "已建模", "停用维度": "已发布"}
    NEGATIVE_ACTIONS = ["停用维度"]
    # 本模块的动作推进状态：提交建模 → 已建模，发布 → 已发布，停用 → 已停用
    TRANSITIONS = {"提交建模": "已建模", "发布维度": "已发布", "停用维度": "已停用"}

    VALID_SCD_TYPES = {"SCD类型0", "SCD类型1", "SCD类型2", "SCD类型3", "SCD类型4", "SCD类型6"}
    CODE_PATTERN = re.compile(r'^DIM_[A-Z0-9_]+$')

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建维度定义，校验编码格式和SCD类型合法性，发布到数据通道。"""
        # 标准化处理
        dim_code = str(values.get("维度编码") or "").strip().upper()
        if dim_code and not self.CODE_PATTERN.match(dim_code):
            dim_code = "DIM_" + re.sub(r'[^A-Z0-9_]', '', dim_code.upper())
            values["维度编码"] = dim_code

        scd_type = str(values.get("SCD类型") or "SCD类型2").strip()
        if scd_type not in self.VALID_SCD_TYPES:
            scd_type = "SCD类型2"
        # 无论调用方是否传值，都回写标准化后的 SCD 类型，避免空值流入下游
        values["SCD类型"] = scd_type

        # 编码唯一性检查：复用维度编码时，旧定义必须已停用
        existing = store.rows(self.MODULE)
        for row in existing:
            if row.get("维度编码") == dim_code:
                if row.get("status") != "已停用":
                    return None, [f"维度编码 {dim_code} 已存在"]
                # 旧版本保留在快照历史里，新版本不会并入它的字段
                row["复用标记"] = f"由定义 {row.get('id')} 停用后复用"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布维度定义到数据通道，让 dim_attr 和 scd_handler 消费
        store.channel.publish("dim.defined", {
            "dim_code": entry["维度编码"],
            "dim_name": entry["维度名称"],
            "scd_type": entry["SCD类型"],
            "primary_key": entry["主键定义"],
            "entry_id": entry["id"],
        })

        # 注册数据产物
        store.artifacts.register(
            "dim_definition", "dim_definition",
            f"dim_{entry['id']}",
            {"dim_code": entry["维度编码"], "dim_name": entry["维度名称"], "scd_type": entry["SCD类型"]},
            source_modules=["dim_definition"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        # 状态流转只在服务层推进，避免把动作前的状态写回记录
        target = self.TRANSITIONS.get(action)
        if target is not None:
            entry["status"] = target
            entry["pending"] = target != self.STATUS_ORDER[-1]
            entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        if action == "发布维度":
            # 生成维度属性发布快照：统一发布依据，供 dim_attr 校验归属并登记
            snapshot = store.publish.open_snapshot(entry)
            entry["发布快照ID"] = snapshot["snapshot_id"]
            entry["发布版本"] = snapshot["version"]
            entry["发布状态"] = snapshot["status"]
            # 发布维度时，通知下游模块处理维度发布事件
            store.channel.publish("dim.published", {
                "dim_code": entry["维度编码"],
                "dim_name": entry["维度名称"],
                "scd_type": entry["SCD类型"],
                "primary_key": entry["主键定义"],
                "primary_key_type": snapshot["primary_key_type"],
                "publish_snapshot_id": snapshot["snapshot_id"],
                "publish_version": snapshot["version"],
                "snapshot_status": snapshot["status"],
                "missing_required_fields": snapshot["missing_required_fields"],
                "granularity": entry["粒度说明"],
                "source": entry["数据来源"],
                "refresh_frequency": entry["刷新频率"],
                "entry_id": entry["id"],
            })
            # 为维度属性建立关联消费标记
            _auto_associate_attributes(entry["维度编码"], entry_id)
            # 为SCD策略建立自动适配
            _auto_create_scd_strategy(entry)
        elif action == "停用维度":
            store.channel.publish("dim.deactivated", {
                "dim_code": entry["维度编码"],
                "entry_id": entry["id"],
            })
            # 过期相关 topic 上未消费的消息
            store.channel.expire_topic("dim.defined")
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 增强快照：附加已发布维度关联的属性信息
        published_dims = []
        for row in store.rows(self.MODULE):
            if row.get("status") == "已发布":
                published_dims.append({
                    "dim_code": row.get("维度编码"),
                    "dim_name": row.get("维度名称"),
                    "scd_type": row.get("SCD类型"),
                })
        base["published_dims"] = published_dims
        base["channel_stats"] = store.channel.topic_stats().get("dim.defined", {})
        return base


def _auto_associate_attributes(dim_code: str, dim_entry_id: int) -> None:
    """自动关联维度属性：为已存在的属性匹配到新发布的维度。"""
    attr_rows = store.rows("dim_attr")
    for attr in attr_rows:
        if attr.get("所属维度") == dim_code:
            # 发送属性关联事件
            store.channel.publish("attr.linked", {
                "dim_code": dim_code,
                "attr_name": attr.get("属性名称"),
                "data_type": attr.get("数据类型"),
                "attr_id": attr.get("id"),
                "source_dim_id": dim_entry_id,
            })


def _auto_create_scd_strategy(dim_def: dict[str, Any]) -> None:
    """根据维度定义的SCD类型自动生成SCD处理策略建议。"""
    scd_type = dim_def.get("SCD类型", "SCD类型2")
    dim_code = dim_def.get("维度编码", "")
    strategy_name = f"{dim_code}_{scd_type.replace('类型', '')}_auto"

    # 检查是否已有同名策略
    existing = store.rows("scd_handler")
    for row in existing:
        if row.get("策略名称") == strategy_name:
            return

    # 发布SCD策略建议到通道
    store.channel.publish("scd.strategy_suggested", {
        "dim_code": dim_code,
        "scd_type": scd_type,
        "strategy_name": strategy_name,
        "dim_name": dim_def.get("维度名称"),
        "primary_key": dim_def.get("主键定义"),
        "source_dim_id": dim_def.get("id"),
    })