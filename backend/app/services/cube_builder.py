"""多维立方服务：构建多维分析立方体，定义维度组合、预计算策略和查询优化。

增强能力：
- 消费 fact.published 通道获取维度组合信息
- 预计算策略选择（全部/增量/核心组合）
- 分区方式推荐
- Cube构建进度追踪
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

PRECOMPUTE_STRATEGIES = {"全部预计算", "增量预计算", "核心组合", "无预计算"}
PARTITION_METHODS = {"按日期分区", "按周分区", "按月分区", "按维度分区", "无分区"}


class CubeBuilderService(BaseModuleService):
    MODULE = "cube_builder"
    ENTITY = "Cube定义"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["Cube名称", "源事实表", "维度组合", "度量集合", "预计算策略", "分区方式", "查询优化"]
    REQUIRED_FIELDS = ["Cube名称", "源事实表", "维度组合"]
    STATUS_ORDER = ["草稿", "计算中", "已就绪", "已失效"]
    ACTION_RULES = {"构建Cube": "草稿", "失效Cube": "已就绪"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        strategy = str(values.get("预计算策略", "")).strip()
        if strategy not in PRECOMPUTE_STRATEGIES:
            values["预计算策略"] = "全部预计算"

        partition = str(values.get("分区方式", "")).strip()
        if partition not in PARTITION_METHODS:
            values["分区方式"] = "按日期分区"

        if not values.get("查询优化"):
            values["查询优化"] = "复合索引"

        # 消费事实发布通道
        store.channel.consume("fact.published", "cube_builder")
        store.channel.consume("measure.ready", "cube_builder")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("cube.defined", {
            "cube_name": entry["Cube名称"],
            "source_fact": entry.get("源事实表"),
            "strategy": entry.get("预计算策略"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "cube_builder", "cube_def",
            f"cube_{entry['id']}",
            {"cube_name": entry["Cube名称"], "source_fact": entry.get("源事实表")},
            ["fact_design", "measure_config", "cube_builder"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "构建Cube":
            entry["status"] = "计算中"
            entry["pending"] = True
            result = _simulate_cube_build(entry)
            store.channel.publish("cube.built", {
                "cube_name": entry["Cube名称"],
                "cells_precomputed": result["cells"],
                "entry_id": entry_id,
            })
            # 构建完成后设为已就绪
            entry["status"] = "已就绪"
            entry["pending"] = False
        elif action == "失效Cube":
            store.channel.publish("cube.invalidated", {
                "cube_name": entry["Cube名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        ready = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已就绪")
        base["ready_cubes"] = ready
        return base


def _simulate_cube_build(entry: dict[str, Any]) -> dict[str, Any]:
    return {"cells": 10000, "duration_ms": 5000, "success": True}