"""维度属性发布快照：维度定义与维度属性登记的统一发布依据。

每次维度发布都会生成一份发布快照，快照记录：

- 维度编码、发布版本、来源维度定义
- 主键字段与主键字段类型
- 该版本可用的属性字段（复用维度编码时不会并入上一版本的字段）
- 必填字段是否齐全、快照生效区间

维度属性登记只消费「已生效」的发布快照：按维度编码校验属性归属，检查同名字段
和主键字段类型，并把登记所依据的快照版本写回属性记录。新快照切换完成前，
原生效快照继续作为查询和登记依据。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

# 快照状态：待确认 = 必填字段未齐全；已就绪 = 必填齐全、等待切换；
# 生效 = 正在供应查询；已切换 = 历史版本
STATUS_ACTIVE = "生效"
STATUS_READY = "已就绪"
STATUS_PENDING = "待确认"
STATUS_SWITCHED = "已切换"

NEVER_EXPIRE = "9999-12-31 23:59:59"
_TYPE_PARAM = re.compile(r"\(.*\)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_type(value: Any) -> str:
    """统一类型写法，忽略大小写和空格，例如 ``varchar ( 32 )`` → ``VARCHAR(32)``。"""
    return re.sub(r"\s+", "", str(value or "")).upper()


def parse_primary_key(primary_key: Any) -> list[dict[str, str]]:
    """解析主键定义，例如 ``product_id VARCHAR(32)``。

    支持逗号分隔的复合主键：``product_id BIGINT, region_code VARCHAR(16)``，
    类型里的逗号（如 ``DECIMAL(18,2)``）不会被当成字段分隔。
    未写字段类型时 ``type`` 为空，快照会因此处于待确认状态。
    """
    text = str(primary_key or "")
    parts: list[str] = []
    depth = 0
    buffer: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char in ",，" and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))

    fields: list[dict[str, str]] = []
    for part in parts:
        tokens = part.strip().split()
        if not tokens:
            continue
        fields.append({
            "field": tokens[0],
            "type": " ".join(tokens[1:]) if len(tokens) > 1 else "",
        })
    return fields


def is_nullable(attribute: dict[str, Any]) -> bool:
    """判断属性是否可空。

    优先读取显式的 ``是否可空``/``是否必填``；都没有时以默认值判断：
    有默认值的字段可以补录（可空），没有默认值的字段在发布时必须齐全。
    """
    if "是否可空" in attribute:
        return str(attribute.get("是否可空")).strip() == "是"
    if "是否必填" in attribute:
        return str(attribute.get("是否必填")).strip() == "否"
    return bool(str(attribute.get("默认值") or "").strip())


class PublishSnapshotRegistry:
    """维度属性发布快照注册表，挂在 ``Store.publish`` 上。"""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._snapshots: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def snapshots_for(self, dim_code: str, definition_id: int | None = None) -> list[dict[str, Any]]:
        items = [
            snapshot for snapshot in self._snapshots.values()
            if snapshot["dim_code"] == dim_code
            and (definition_id is None or snapshot["definition_id"] == definition_id)
        ]
        return sorted(items, key=lambda item: item["version_no"])

    def active(self, dim_code: str) -> dict[str, Any] | None:
        """当前供应查询的生效快照。"""
        for snapshot in self.snapshots_for(dim_code):
            if snapshot["status"] == STATUS_ACTIVE and snapshot["serves_queries"]:
                return snapshot
        return None

    def latest(self, dim_code: str) -> dict[str, Any] | None:
        """最新一份未切换的快照（可能仍在待确认或已就绪）。"""
        for snapshot in reversed(self.snapshots_for(dim_code)):
            if snapshot["status"] != STATUS_SWITCHED:
                return snapshot
        return None

    def snapshot_by_id(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshots.get(snapshot_id)

    def candidate(self, dim_code: str) -> dict[str, Any] | None:
        """等待切换的快照；必填字段齐全后才能用于属性登记。"""
        latest = self.latest(dim_code)
        return latest if latest is not None and latest["status"] != STATUS_ACTIVE else None

    # ------------------------------------------------------------------
    # 发布：生成快照
    # ------------------------------------------------------------------
    def open_snapshot(self, definition: dict[str, Any]) -> dict[str, Any]:
        """按维度定义发布一份快照。

        同一个维度定义重复发布时复用已有快照；只有新的维度定义（例如停用后
        复用维度编码）才会产生新版本。新版本只收录属于本版本的字段，
        上一版本的字段不会并入。
        """
        dim_code = str(definition.get("维度编码") or "").strip()
        existing = self.snapshots_for(dim_code, definition.get("id"))
        if existing:
            snapshot = existing[-1]
            self.refresh(dim_code)
            return snapshot

        history = self.snapshots_for(dim_code)
        version_no = len(history) + 1
        version = f"v{version_no}"
        primary_key_fields = parse_primary_key(definition.get("主键定义"))

        snapshot = {
            "snapshot_id": f"DPS-{dim_code}-{version}-{uuid.uuid4().hex[:6].upper()}",
            "dim_code": dim_code,
            "dim_name": definition.get("维度名称", ""),
            "version": version,
            "version_no": version_no,
            "definition_id": definition.get("id"),
            "primary_key_fields": primary_key_fields,
            "primary_key_field": primary_key_fields[0]["field"] if primary_key_fields else "",
            "primary_key_type": primary_key_fields[0]["type"] if primary_key_fields else "",
            "required_fields": [],
            "missing_required_fields": [],
            "status": STATUS_PENDING,
            "serves_queries": False,
            "effective_from": None,
            "effective_to": None,
            "created_at": _now(),
            "activated_at": None,
            "switched_at": None,
            "source": "dim_definition.发布维度",
        }
        self._snapshots[snapshot["snapshot_id"]] = snapshot

        # 只绑定属于本版本的字段；v1 收录尚无版本记录的存量字段，
        # v2 及以后不继承上一版本，必须由新登记补入。
        self._bind_version_attributes(snapshot, first_version=version_no == 1)
        self._freeze_required_fields(snapshot)
        self._evaluate(snapshot)
        if version_no == 1 and snapshot["status"] == STATUS_READY:
            # 首版发布即可直接供应查询
            self._activate(snapshot)
        elif snapshot["status"] == STATUS_READY and self.active(dim_code) is None:
            # 复用维度编码的新版本：必填齐全后仍要等切换，切换前没有生效版本
            self._stamp(snapshot)
        else:
            # 必填字段未齐全：旧生效快照继续供应查询，不做切换。
            self._stamp(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 切换 / 刷新
    # ------------------------------------------------------------------
    def activate(self, dim_code: str) -> tuple[bool, str]:
        """尝试把最新待切换快照切换为生效快照。"""
        latest = self.latest(dim_code)
        if latest is None:
            return False, f"维度 {dim_code} 还没有发布快照"
        if latest["status"] == STATUS_ACTIVE:
            return False, f"发布快照 {latest['version']} 已生效，无需切换"
        self._evaluate(latest)
        if latest["status"] == STATUS_PENDING:
            return False, (
                f"发布快照 {latest['version']} 缺少必填字段："
                f"{'、'.join(latest['missing_required_fields'])}"
            )
        self._activate(latest)
        return True, f"已切换到发布快照 {latest['version']}"

    def refresh(self, dim_code: str) -> dict[str, Any] | None:
        """重新核对最新的待切换快照，必填字段齐全后完成切换。"""
        latest = self.latest(dim_code)
        if latest is not None:
            if latest["status"] not in (STATUS_SWITCHED, STATUS_ACTIVE):
                self._evaluate(latest)
            if latest["status"] == STATUS_READY:
                self._activate(latest)
            elif latest["status"] != STATUS_ACTIVE:
                self._stamp(latest)
        return self.active(dim_code) or latest

    # ------------------------------------------------------------------
    # 属性登记校验
    # ------------------------------------------------------------------
    def validate_registration(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
        """按生效发布快照校验属性登记。

        返回 ``(快照, 错误列表, 快照内字段)``；有错误时快照不可用于登记。
        """
        dim_code = str(values.get("所属维度") or "").strip()
        attr_name = str(values.get("属性名称") or "").strip()
        data_type = str(values.get("数据类型") or "").strip()

        active = self.active(dim_code)
        if active is None:
            snapshots = self.snapshots_for(dim_code)
            pending = self.latest(dim_code)
            if pending is not None:
                missing = "、".join(pending["missing_required_fields"]) or "必填字段待确认"
                return None, [
                    f"维度 {dim_code} 的发布快照 {pending['version']} 尚未生效，缺少必填字段：{missing}"
                ], []
            if snapshots:
                return None, [f"维度 {dim_code} 没有已生效的发布快照，属性登记不可用"], []
            known = any(
                str(row.get("维度编码") or "").strip() == dim_code
                for row in self._store.rows("dim_definition")
            )
            if known:
                return None, [f"维度 {dim_code} 尚未发布，属性登记只能使用已发布的维度快照"], []
            return None, [f"维度编码 {dim_code} 不存在，无法确认属性归属"], []

        if active["dim_code"] != dim_code:
            return None, [f"属性归属校验失败：快照属于 {active['dim_code']}，登记维度为 {dim_code}"], []

        fields = self._fields(active)
        errors: list[str] = []

        if attr_name and any(field["属性名称"] == attr_name for field in fields):
            errors.append(
                f"属性名称 {attr_name} 已在维度 {dim_code} 的发布快照 "
                f"{active['version']}（{active['snapshot_id']}）中登记"
            )

        primary_key = {item["field"]: item for item in active["primary_key_fields"]}
        if attr_name in primary_key:
            expected = primary_key[attr_name]["type"]
            if not expected:
                errors.append(
                    f"主键字段 {attr_name} 在发布快照 {active['version']} 中缺少字段类型"
                )
            elif normalize_type(data_type) != normalize_type(expected):
                errors.append(
                    f"主键字段 {attr_name} 的发布类型为 {expected}，"
                    f"登记类型 {data_type} 不一致"
                )

        return (None, errors, []) if errors else (active, [], fields)

    def bind_attribute(self, attribute: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """把属性记录绑定到登记所依据的快照，写回归属与生效区间。"""
        attribute["属性归属"] = snapshot["dim_code"]
        attribute["发布快照ID"] = snapshot["snapshot_id"]
        attribute["发布快照版本"] = snapshot["version"]
        attribute["维度版本"] = snapshot["version"]
        attribute["生效区间"] = self._interval(snapshot)
        if not attribute.get("登记时间"):
            attribute["登记时间"] = _now()
        if attribute.get("status") == "已确认" and snapshot.get("effective_from"):
            attribute.setdefault("属性可用时间", snapshot["effective_from"])

    def on_attribute_confirmed(self, attribute: dict[str, Any]) -> None:
        """属性确认后更新可用时间；若补齐了必填字段则完成快照切换。"""
        attribute["属性可用时间"] = attribute.get("属性可用时间") or _now()
        snapshot = self._snapshots.get(str(attribute.get("发布快照ID") or ""))
        if snapshot is None:
            return
        if snapshot["status"] == STATUS_SWITCHED:
            return
        if snapshot["status"] == STATUS_ACTIVE:
            # 生效快照内的迟到可空字段，确认后对查询立即可见
            self._stamp(snapshot)
            return
        self._evaluate(snapshot)
        if snapshot["status"] == STATUS_READY:
            # 必填字段已补齐，完成切换：新快照接手查询，旧快照此时才收回
            self._activate(snapshot)
        else:
            self._stamp(snapshot)

    # ------------------------------------------------------------------
    # 发布视图
    # ------------------------------------------------------------------
    def publish_view(self, dim_code: str) -> dict[str, Any] | None:
        """维度发布视图：快照版本、字段可用时间和登记用到的版本。"""
        snapshots = self.snapshots_for(dim_code)
        definitions = [
            row for row in self._store.rows("dim_definition")
            if str(row.get("维度编码") or "").strip() == dim_code
        ]
        if not snapshots and not definitions:
            return None

        for snapshot in snapshots:
            self._stamp(snapshot)

        active = self.active(dim_code)
        candidate = self.candidate(dim_code)
        latest = self.latest(dim_code)
        focus = active or latest
        registrations = self.registration_records(dim_code)

        return {
            "dim_code": dim_code,
            "dim_name": (active or latest or {}).get("dim_name", ""),
            "active_version": active["version"] if active else "",
            "active_snapshot_id": active["snapshot_id"] if active else "",
            "candidate_version": candidate["version"] if candidate else "",
            "candidate_status": candidate["status"] if candidate else "",
            "candidate_missing_fields": list(candidate["missing_required_fields"]) if candidate else [],
            # 属性登记只使用必填字段齐全的生效快照
            "can_register": active is not None,
            "registration_basis": active["version"] if active else "",
            "primary_key": {
                "field": focus["primary_key_field"] if focus else "",
                "type": focus["primary_key_type"] if focus else "",
                "fields": list(focus["primary_key_fields"]) if focus else [],
            },
            # 查询侧继续使用生效快照的字段，切换完成前不变
            "fields": self._fields(active) if active else (self._fields(latest) if latest else []),
            "available_fields": [
                field["属性名称"] for field in (self._fields(active) if active else [])
                if field["是否可用"] == "是"
            ],
            "pending_fields": [
                field["属性名称"] for field in (self._fields(active) if active else [])
                if field["是否可用"] != "是"
            ],
            "candidate_fields": self._fields(candidate) if candidate else [],
            "incoming_fields": self._incoming_fields(active, latest),
            "field_timeline": self._field_timeline(snapshots),
            "snapshots": [self._serialize_snapshot(snapshot) for snapshot in snapshots],
            "registrations": registrations,
            "registration_versions": self._registration_versions(registrations),
        }

    def list_views(self) -> list[dict[str, Any]]:
        codes: list[str] = [snapshot["dim_code"] for snapshot in self._snapshots.values()]
        codes.extend(
            str(row.get("维度编码") or "").strip()
            for row in self._store.rows("dim_definition")
            if row.get("status") == "已发布"
        )
        views = []
        for code in sorted({code for code in codes if code}):
            view = self.publish_view(code)
            if view is not None:
                views.append(view)
        return views

    def registration_records(self, dim_code: str) -> list[dict[str, Any]]:
        """属性登记记录：保留每条记录所依据的维度版本。"""
        records = []
        for row in self._store.rows("dim_attr"):
            if str(row.get("所属维度") or "").strip() != dim_code:
                continue
            version = str(row.get("发布快照版本") or "").strip()
            if not version:
                continue
            records.append({
                "属性名称": row.get("属性名称", ""),
                "数据类型": row.get("数据类型", ""),
                "发布快照版本": version,
                "发布快照ID": row.get("发布快照ID", ""),
                "登记时间": row.get("登记时间", ""),
                "可用时间": row.get("属性可用时间", ""),
                "状态": row.get("status", ""),
            })
        return records

    def summary(self) -> dict[str, Any]:
        by_status = {STATUS_ACTIVE: 0, STATUS_READY: 0, STATUS_PENDING: 0, STATUS_SWITCHED: 0}
        for snapshot in self._snapshots.values():
            by_status[snapshot["status"]] = by_status.get(snapshot["status"], 0) + 1
        return {
            "total": len(self._snapshots),
            "by_status": by_status,
            "dimensions": len({snapshot["dim_code"] for snapshot in self._snapshots.values()}),
        }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _bind_version_attributes(self, snapshot: dict[str, Any], first_version: bool) -> None:
        """把字段归入快照版本。

        只有首版才接收没有版本记录的存量字段；复用维度编码的新版本不会
        从上一版本继承字段。
        """
        version = snapshot["version"]
        for attribute in self._store.rows("dim_attr"):
            if str(attribute.get("所属维度") or "").strip() != snapshot["dim_code"]:
                continue
            recorded = str(attribute.get("发布快照版本") or "").strip()
            if recorded == version or (first_version and not recorded):
                attribute["属性归属"] = snapshot["dim_code"]
                attribute["发布快照ID"] = snapshot["snapshot_id"]
                attribute["发布快照版本"] = version
                attribute["维度版本"] = version
                if not attribute.get("登记时间"):
                    attribute["登记时间"] = snapshot["created_at"]

    def _version_attributes(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            attribute for attribute in self._store.rows("dim_attr")
            if str(attribute.get("所属维度") or "").strip() == snapshot["dim_code"]
            and str(attribute.get("发布快照版本") or "").strip() == snapshot["version"]
        ]

    def _freeze_required_fields(self, snapshot: dict[str, Any]) -> None:
        """冻结发布时的必填字段，后续补录字段不改变本次发布要求。"""
        required = [item["field"] for item in snapshot["primary_key_fields"]]
        for attribute in self._version_attributes(snapshot):
            if not is_nullable(attribute):
                required.append(str(attribute.get("属性名称") or ""))
        snapshot["required_fields"] = _dedupe(required)

    def _evaluate(self, snapshot: dict[str, Any]) -> None:
        # 已经供应查询的快照不因后续登记/下线回退为待确认
        if snapshot["status"] == STATUS_SWITCHED or snapshot["serves_queries"]:
            return
        primary_key_names = {item["field"] for item in snapshot["primary_key_fields"]}
        fields = {field["属性名称"]: field for field in self._fields(snapshot)}
        missing: list[str] = []

        for item in snapshot["primary_key_fields"]:
            if not item["type"]:
                missing.append(f"主键字段 {item['field']} 缺少字段类型")

        for name in snapshot["required_fields"]:
            if name in primary_key_names:
                continue
            field = fields.get(name)
            if field is None:
                missing.append(f"必填字段 {name} 未登记")
            elif field["是否已确认"] != "是":
                missing.append(f"必填字段 {name} 待确认")

        snapshot["missing_required_fields"] = _dedupe(missing)
        snapshot["status"] = STATUS_PENDING if missing else STATUS_READY

    def _activate(self, snapshot: dict[str, Any]) -> None:
        now = _now()
        previous = self.active(snapshot["dim_code"])
        if previous is not None and previous["snapshot_id"] != snapshot["snapshot_id"]:
            # 切换完成才收回旧快照的供应权；此前旧快照一直供应查询。
            previous["status"] = STATUS_SWITCHED
            previous["serves_queries"] = False
            previous["effective_to"] = now
            previous["switched_at"] = now
            self._stamp(previous)

        snapshot["status"] = STATUS_ACTIVE
        snapshot["serves_queries"] = True
        snapshot["effective_from"] = snapshot.get("effective_from") or now
        snapshot["effective_to"] = None
        snapshot["activated_at"] = now
        self._stamp(snapshot)

    def _stamp(self, snapshot: dict[str, Any]) -> None:
        # 归属与生效区间跟随所属快照：快照确认（生效/切换）后一起刷新。
        interval = self._interval(snapshot)
        for attribute in self._version_attributes(snapshot):
            attribute["属性归属"] = snapshot["dim_code"]
            attribute["发布快照ID"] = snapshot["snapshot_id"]
            attribute["发布快照版本"] = snapshot["version"]
            attribute["维度版本"] = snapshot["version"]
            attribute["生效区间"] = interval
            if not attribute.get("登记时间"):
                attribute["登记时间"] = snapshot["created_at"]
            if attribute.get("status") == "已确认" and not attribute.get("属性可用时间"):
                attribute["属性可用时间"] = snapshot.get("effective_from") or ""

    def _interval(self, snapshot: dict[str, Any]) -> str:
        start = snapshot.get("effective_from") or "待生效"
        end = snapshot.get("effective_to") or NEVER_EXPIRE
        return f"{start} ~ {end}"

    def _fields(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        primary_key_names = {item["field"] for item in snapshot["primary_key_fields"]}
        result: list[dict[str, Any]] = []
        for attribute in self._version_attributes(snapshot):
            name = str(attribute.get("属性名称") or "")
            confirmed = attribute.get("status") == "已确认"
            # 只有确认动作才会写入可用时间，所以可用时间就是字段对查询可见的时刻
            available_at = str(attribute.get("属性可用时间") or "")
            registered_at = str(attribute.get("登记时间") or "")
            usable = (
                confirmed
                and bool(registered_at)
                and snapshot["status"] != STATUS_PENDING
                and (not available_at or _time_le(available_at, _now()))
            )
            result.append({
                "属性名称": name,
                "数据类型": attribute.get("数据类型", ""),
                "是否可空": "是" if is_nullable(attribute) else "否",
                "是否必填": "是" if name in snapshot["required_fields"] and name not in primary_key_names else "否",
                "是否已确认": "是" if confirmed else "否",
                "是否可用": "是" if usable else "否",
                "主键字段": name in primary_key_names,
                "登记时间": registered_at,
                # 可用时间只在真正可用后展示，避免把计划时间当成已可用
                "可用时间": available_at if usable else "",
                "计划可用时间": available_at,
                "归属维度": attribute.get("属性归属", snapshot["dim_code"]),
                "发布快照版本": snapshot["version"],
                "发布快照ID": snapshot["snapshot_id"],
            })
        return result

    def _serialize_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "version": snapshot["version"],
            "definition_id": snapshot["definition_id"],
            "dim_code": snapshot["dim_code"],
            "dim_name": snapshot["dim_name"],
            "status": snapshot["status"],
            "serves_queries": snapshot["serves_queries"],
            "primary_key_field": snapshot["primary_key_field"],
            "primary_key_type": snapshot["primary_key_type"],
            "primary_key_fields": list(snapshot["primary_key_fields"]),
            "required_fields": list(snapshot["required_fields"]),
            "missing_required_fields": list(snapshot["missing_required_fields"]),
            "effective_from": snapshot["effective_from"] or "",
            "effective_to": snapshot["effective_to"] or "",
            "created_at": snapshot["created_at"],
            "activated_at": snapshot["activated_at"] or "",
            "switched_at": snapshot["switched_at"] or "",
            "fields": self._fields(snapshot),
        }

    def _incoming_fields(
        self, active: dict[str, Any] | None, candidate: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """切换后才会出现的新字段，供发布视图回答「何时显示新字段」。"""
        if candidate is None:
            return []
        active_names = {
            field["属性名称"] for field in (self._fields(active) if active else [])
        }
        incoming = []
        for field in self._fields(candidate):
            if field["属性名称"] in active_names:
                continue
            if candidate["status"] == STATUS_ACTIVE:
                # 已经接手的版本里，字段按确认时间逐步开放
                visible = (
                    "确认后即可用"
                    if field["是否已确认"] == "是"
                    else "待确认，确认后可用"
                )
            elif candidate["status"] == STATUS_READY:
                visible = f"{candidate['version']} 切换为生效快照后显示"
            else:
                visible = f"{candidate['version']} 补齐必填字段并切换后显示"
            incoming.append({
                "属性名称": field["属性名称"],
                "数据类型": field["数据类型"],
                "是否可空": field["是否可空"],
                "是否必填": field["是否必填"],
                "是否已确认": field["是否已确认"],
                "版本": candidate["version"],
                "可见条件": visible,
            })
        return incoming

    def _field_timeline(self, snapshots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        timeline: list[dict[str, Any]] = []
        for snapshot in snapshots:
            for field in self._fields(snapshot):
                if field["是否可用"] == "是":
                    state = "已可用"
                elif snapshot["status"] == STATUS_PENDING:
                    state = "待切换"
                else:
                    state = "待确认"
                timeline.append({
                    "属性名称": field["属性名称"],
                    "版本": snapshot["version"],
                    "状态": state,
                    "可用时间": field["可用时间"],
                    "计划可用时间": field["计划可用时间"],
                    "是否可空": field["是否可空"],
                })
        return timeline

    @staticmethod
    def _registration_versions(records: list[dict[str, Any]]) -> list[str]:
        versions = {str(record.get("发布快照版本") or "") for record in records}
        return sorted((version for version in versions if version), key=_version_key)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _version_key(version: str) -> int:
    match = re.search(r"(\d+)", version)
    return int(match.group(1)) if match else 0


def _time_le(left: str, right: str) -> bool:
    """比较两个 ISO 时间字符串，兼容不同精度。"""
    return str(left)[:19] <= str(right)[:19]
