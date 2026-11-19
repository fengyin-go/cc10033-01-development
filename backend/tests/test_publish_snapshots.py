"""维度属性发布快照测试：发布依据、归属校验、版本隔离与切换。"""
from __future__ import annotations

import pytest

from app.seed import SEED_ROWS
from app.services.dim_attr import DimAttrService
from app.services.dim_definition import DimDefinitionService
from app.store import store
from app.store import ArtifactRegistry, DataChannel, FailureRecovery, SnapshotEngine


@pytest.fixture
def services():
    """每个测试重置全局 store：服务模块持有的是同一个对象引用。"""
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store.channel = DataChannel()
    store.recovery = FailureRecovery()
    store.snapshot = SnapshotEngine()
    store.artifacts = ArtifactRegistry()
    store.publish = type(store.publish)(store)
    for row in store.rows("dim_definition"):
        if row.get("status") == "已发布":
            store.publish.open_snapshot(row)
    try:
        yield store, DimDefinitionService(), DimAttrService()
    finally:
        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        store.channel = DataChannel()
        store.recovery = FailureRecovery()
        store.snapshot = SnapshotEngine()
        store.artifacts = ArtifactRegistry()
        store.publish = type(store.publish)(store)
        for row in store.rows("dim_definition"):
            if row.get("status") == "已发布":
                store.publish.open_snapshot(row)


def _publish(defs: DimDefinitionService, **values) -> dict:
    entry, errors = defs.create_entry(values)
    assert errors == []
    published, _ = defs.run_action(entry["id"], "发布维度")
    assert published is not None
    return published


def test_publish_creates_snapshot_with_primary_key_type(services) -> None:
    store, defs, _ = services
    entry = _publish(
        defs,
        **{
            "维度编码": "DIM_ORDER",
            "维度名称": "订单维度",
            "主键定义": "order_id BIGINT",
            "SCD类型": "SCD类型2",
            "粒度说明": "订单级",
            "数据来源": "oms",
            "刷新频率": "每日",
        },
    )
    assert entry["status"] == "已发布"
    assert entry["发布版本"] == "v1"

    view = store.publish.publish_view("DIM_ORDER")
    assert view["active_version"] == "v1"
    assert view["primary_key"]["field"] == "order_id"
    assert view["primary_key"]["type"] == "BIGINT"
    assert view["can_register"] is True


def test_registration_rejects_duplicate_field_in_same_snapshot(services) -> None:
    store, defs, attrs = services
    _publish(defs, **{"维度编码": "DIM_ORDER", "维度名称": "订单维度",
                      "主键定义": "order_id BIGINT"})
    first, errors = attrs.create_entry({"属性名称": "order_status", "所属维度": "DIM_ORDER",
                                        "数据类型": "VARCHAR(16)"})
    assert errors == []
    assert first["发布快照版本"] == "v1"

    duplicate, errors = attrs.create_entry({"属性名称": "order_status", "所属维度": "DIM_ORDER",
                                            "数据类型": "VARCHAR(16)"})
    assert duplicate is None
    assert "已在维度 DIM_ORDER 的发布快照 v1" in errors[0]


def test_registration_checks_primary_key_type(services) -> None:
    _, defs, attrs = services
    _publish(defs, **{"维度编码": "DIM_ORDER", "维度名称": "订单维度",
                      "主键定义": "order_id BIGINT"})

    mismatch, errors = attrs.create_entry({"属性名称": "order_id", "所属维度": "DIM_ORDER",
                                           "数据类型": "VARCHAR(16)"})
    assert mismatch is None
    assert "发布类型为 BIGINT" in errors[0]

    matched, errors = attrs.create_entry({"属性名称": "order_id", "所属维度": "DIM_ORDER",
                                          "数据类型": "bigint"})
    assert errors == []
    assert matched["发布快照版本"] == "v1"


def test_registration_requires_published_dimension(services) -> None:
    _, defs, attrs = services
    defs.create_entry({"维度编码": "DIM_DRAFT", "维度名称": "未发布维度",
                       "主键定义": "draft_id INT"})
    entry, errors = attrs.create_entry({"属性名称": "draft_name", "所属维度": "DIM_DRAFT",
                                        "数据类型": "VARCHAR(32)"})
    assert entry is None
    assert "尚未发布" in errors[0]

    unknown, errors = attrs.create_entry({"属性名称": "x", "所属维度": "DIM_UNKNOWN",
                                          "数据类型": "INT"})
    assert unknown is None
    assert "不存在" in errors[0]


def test_pending_snapshot_from_missing_primary_key_type(services) -> None:
    store, defs, attrs = services
    # 主键没有字段类型：必填字段不齐，快照停在待确认，不能用于登记
    _publish(defs, **{"维度编码": "DIM_SHOP", "维度名称": "门店维度",
                      "主键定义": "store_id"})

    view = store.publish.publish_view("DIM_SHOP")
    assert view["can_register"] is False
    assert view["candidate_status"] == "待确认"
    assert any("缺少字段类型" in item for item in view["candidate_missing_fields"])

    blocked, errors = attrs.create_entry({"属性名称": "store_city", "所属维度": "DIM_SHOP",
                                          "数据类型": "VARCHAR(32)"})
    assert blocked is None
    assert "尚未生效" in errors[0]


def test_late_nullable_field_appears_after_confirmation(services) -> None:
    store, defs, attrs = services
    _publish(defs, **{"维度编码": "DIM_SKU", "维度名称": "产品维度",
                      "主键定义": "product_id VARCHAR(32)"})
    attrs.create_entry({"属性名称": "product_name", "所属维度": "DIM_SKU",
                        "数据类型": "VARCHAR(256)"})

    late, errors = attrs.create_entry({
        "属性名称": "product_brand", "所属维度": "DIM_SKU",
        "数据类型": "VARCHAR(64)", "默认值": "未知",
    })
    assert errors == []
    # 发布后补录的可空字段先登记、仍待确认，尚未对查询开放
    view = store.publish.publish_view("DIM_SKU")
    assert "product_brand" not in view["available_fields"]
    assert "product_brand" in view["pending_fields"]

    attrs.run_action(late["id"], "确认属性")
    view = store.publish.publish_view("DIM_SKU")
    assert "product_brand" in view["available_fields"]
    timeline = {item["属性名称"]: item for item in view["field_timeline"]}
    assert timeline["product_brand"]["状态"] == "已可用"
    assert timeline["product_brand"]["可用时间"]


def test_available_snapshot_keeps_serving_until_switch(services) -> None:
    store, defs, attrs = services
    _publish(defs, **{"维度编码": "DIM_SHOP", "维度名称": "门店维度",
                      "主键定义": "store_id INT"})
    # 发布后才登记、尚未确认的必填字段（无默认值）
    attr, errors = attrs.create_entry({"属性名称": "store_region", "所属维度": "DIM_SHOP",
                                       "数据类型": "VARCHAR(32)"})
    assert errors == []

    # 生效快照继续供应查询，迟到的字段在确认前不可见
    view = store.publish.publish_view("DIM_SHOP")
    assert view["active_version"] == "v1"
    assert "store_region" in view["pending_fields"]

    attrs.run_action(attr["id"], "确认属性")
    view = store.publish.publish_view("DIM_SHOP")
    assert view["active_version"] == "v1"
    assert view["can_register"] is True
    assert "store_region" in view["available_fields"]


def test_reused_dim_code_does_not_inherit_previous_fields(services) -> None:
    store, defs, attrs = services
    first = _publish(defs, **{"维度编码": "DIM_SKU", "维度名称": "产品维度",
                              "主键定义": "product_id VARCHAR(32)"})
    attrs.create_entry({"属性名称": "product_name", "所属维度": "DIM_SKU",
                        "数据类型": "VARCHAR(256)"})

    # 未停用前不能复用编码
    blocked, errors = defs.create_entry({"维度编码": "DIM_SKU", "维度名称": "重复",
                                         "主键定义": "product_id VARCHAR(64)"})
    assert blocked is None
    assert "已存在" in errors[0]

    defs.run_action(first["id"], "停用维度")
    second = _publish(defs, **{"维度编码": "DIM_SKU", "维度名称": "产品维度v2",
                               "主键定义": "product_id VARCHAR(64)"})
    assert second["发布版本"] == "v2"

    view = store.publish.publish_view("DIM_SKU")
    # 切换完成前旧版本继续供应查询
    assert view["active_version"] == "v1"
    assert view["registration_basis"] == "v1"
    assert view["candidate_version"] == "v2"

    # v2 的字段为空，上一版本字段没有并入
    latest = [item for item in view["snapshots"] if item["version"] == "v2"][0]
    assert latest["fields"] == []
    assert latest["primary_key_type"] == "VARCHAR(64)"

    # 新版本没有继承上一版本字段；切换前登记仍以生效快照 v1 为依据
    late, errors = attrs.create_entry({"属性名称": "sku_name", "所属维度": "DIM_SKU",
                                       "数据类型": "VARCHAR(128)", "是否可空": "是"})
    assert late["发布快照版本"] == "v1"

    ok, _ = store.publish.activate("DIM_SKU")
    assert ok is True
    view = store.publish.publish_view("DIM_SKU")
    assert view["active_version"] == "v2"
    assert view["available_fields"] == []
    snapshots = {item["version"]: item for item in view["snapshots"]}
    assert snapshots["v1"]["status"] == "已切换"
    assert [field["属性名称"] for field in snapshots["v1"]["fields"]] == [
        "product_name", "sku_name",
    ]
    assert "product_name" not in view["available_fields"]


def test_view_lists_registration_versions(services) -> None:
    store, defs, attrs = services
    _publish(defs, **{"维度编码": "DIM_SKU", "维度名称": "产品维度",
                      "主键定义": "product_id VARCHAR(32)"})
    for name in ("product_name", "product_category"):
        attrs.create_entry({"属性名称": name, "所属维度": "DIM_SKU",
                            "数据类型": "VARCHAR(64)"})

    view = store.publish.publish_view("DIM_SKU")
    assert view["registration_versions"] == ["v1"]
    assert {item["属性名称"] for item in view["registrations"]} == {
        "product_name", "product_category",
    }
    assert all(item["发布快照版本"] == "v1" for item in view["registrations"])
