"""召回实物保管地点追溯:重放交接 / 装拆事件,追溯命中召回实物经过的保管地点。

库存实物以 GTIN + AI 21 序列号唯一标识,预先注册进重放内核,其批号(AI 10)、
有效期(AI 17)以库存扫描为基准;事件按 occurred_at 转 UTC 升序重放,同一时刻
依输入顺序。

本模块负责库存 / 事件扫描的解析与库存引用、批号 / 有效期一致性校验,以及命中
召回结果整理;父子边、持有人、成功下标与地点历史统一由 `app.replay.ReplayKernel`
(地点追溯模式)维护:

- 交接(transfer)仅作用于无父件实物;首次成功交接 from_location 须为 null,
  此后须等于当前持有人。根件交接时其全部后代随之换持有人并记录地点;
- 装入(pack)要求双方已有**相同**持有人,且不能自装 / 成环,装入不改持有人;
- 取出(unpack)须为当前直接父子关系,取出后子件保留当时持有人(不重复记地点)。

失败事件由内核保证不留下任何半次更新,后续事件继续从失败前状态处理。
"""

from __future__ import annotations

from datetime import timezone

from fastapi import HTTPException

from .gs1 import ParsedScan, ScanParseError, parse_scan
from .matcher import match_rules
from .models import (
    LocationTraceEventError,
    LocationTraceItem,
    LocationTraceRequest,
    LocationTraceResponse,
    TracePackingEvent,
    TraceTransferEvent,
)
from .replay import (
    CHILD_HAS_PARENT,
    FIRST_TRANSFER_FROM_NOT_NULL,
    HAS_PARENT_TRANSFER,
    HOLDER_DIFFERS,
    HOLDER_MISMATCH,
    HOLDER_MISSING,
    NOT_DIRECT_PARENT,
    PACK_CYCLE,
    ReplayConfig,
    ReplayFailure,
    ReplayKernel,
    SAME_ITEM,
    ItemKey,
    fmt_value,
    item_label,
)


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(status_code=422, detail=reason)


def parse_inventory(raw_scans: list[str]) -> dict[ItemKey, ParsedScan]:
    """解析库存扫描:须全部合法、含 AI 21 且 GTIN+序列号唯一,否则抛 422。"""
    inventory: dict[ItemKey, ParsedScan] = {}
    for index, raw in enumerate(raw_scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            raise _unprocessable(f"库存扫描 {index} 非法: {exc.reason}") from exc
        if parsed.serial is None:
            raise _unprocessable(f"库存扫描 {index} 缺少 AI 21 序列号,无法识别实物")
        key = (parsed.gtin, parsed.serial)
        if key in inventory:
            raise _unprocessable(
                f"库存扫描 {index} 与更早的库存扫描重复: 同一{item_label(key)}"
            )
        inventory[key] = parsed
    return inventory


def _failure_reason(failure: ReplayFailure) -> str:
    """把内核失败结果整理为地点追溯接口的既有文案。"""
    code = failure.code
    if code == HAS_PARENT_TRANSFER:
        assert failure.item is not None and failure.parent is not None
        return (
            f"交接失败:{item_label(failure.item)}当前有直接父件"
            f"{item_label(failure.parent)},仅无父件实物可直接交接"
        )
    if code in (FIRST_TRANSFER_FROM_NOT_NULL, HOLDER_MISMATCH):
        return (
            "交接失败:from_location 须等于当前持有人 "
            f"{failure.expected},实际为 {failure.actual}"
        )
    if code == CHILD_HAS_PARENT:
        assert failure.child is not None and failure.parent is not None
        return (
            f"装入失败:子件{item_label(failure.child)}已有直接父件"
            f"{item_label(failure.parent)},不能重复装入"
        )
    if code == SAME_ITEM:
        return "装入失败:子件不能与父件为同一实物"
    if code == HOLDER_MISSING:
        who = failure.detail  # "父件" 或 "子件"
        missing = failure.parent if who == "父件" else failure.child
        assert missing is not None
        return f"装入失败:{who}{item_label(missing)}尚无成功交接,当前持有人缺失,不能装入"
    if code == HOLDER_DIFFERS:
        return (
            "装入失败:双方当前持有人须相同,"
            f"父件持有人为 '{failure.expected}',子件持有人为 '{failure.actual}'"
        )
    if code == PACK_CYCLE:
        assert failure.child is not None and failure.parent is not None
        return (
            f"装入失败:子件{item_label(failure.child)}是父件{item_label(failure.parent)}"
            "的祖先,装入将形成循环包装"
        )
    if code == NOT_DIRECT_PARENT:
        assert failure.child is not None and failure.parent is not None
        current = failure.item
        if current is None:
            actual = "子件当前无直接父件"
        else:
            actual = f"子件的当前直接父件为{item_label(current)}"
        return f"取出失败:{actual},并非指定的父件{item_label(failure.parent)}"
    return failure.detail or "事件失败"


def trace_locations(request: LocationTraceRequest) -> LocationTraceResponse:
    """重放交接 / 装拆事件,返回命中召回实物经过的地点与失败事件的原因。"""
    inventory = parse_inventory(request.scans)
    kernel = ReplayKernel(ReplayConfig.trace())
    nodes_meta: dict[ItemKey, ParsedScan] = {}
    for key, parsed in inventory.items():
        kernel.register(key, batch=parsed.batch, expiry=parsed.expiry)
        nodes_meta[key] = parsed
    errors: list[LocationTraceEventError] = []

    order = sorted(
        range(len(request.events)),
        key=lambda i: (request.events[i].occurred_at.astimezone(timezone.utc), i),
    )

    def parse_party(
        raw_scan: str, role: str, index: int
    ) -> tuple[ItemKey | None, str | None]:
        """解析事件一端扫描串,并校验库存引用与批号 / 有效期一致性。

        顺序:扫描合法 → 含序列号 → 引用库存实物 → 批号一致 → 有效期一致。
        """
        try:
            parsed = parse_scan(raw_scan)
        except ScanParseError as exc:
            return None, f"事件 {index} 的{role}扫描非法: {exc.reason}"
        if parsed.serial is None:
            return None, f"事件 {index} 的{role}扫描串缺少 AI 21 序列号,无法识别实物"
        key = (parsed.gtin, parsed.serial)
        node = kernel.get(key)
        if node is None:
            return None, f"事件 {index} 的{role}{item_label(key)}不在库存扫描中"
        baseline = nodes_meta[key]
        if parsed.batch != baseline.batch:
            return None, (
                f"事件 {index} 的{role}批号与库存不一致: "
                f"库存为 {fmt_value(baseline.batch)},本次为 {fmt_value(parsed.batch)}"
            )
        if parsed.expiry != baseline.expiry:
            return None, (
                f"事件 {index} 的{role}有效期与库存不一致: "
                f"库存为 {fmt_value(baseline.expiry)},本次为 {fmt_value(parsed.expiry)}"
            )
        return key, None

    for index in order:
        event = request.events[index]

        if isinstance(event, TraceTransferEvent):
            key, err = parse_party(event.scan, "扫描", index)
            if err is not None:
                errors.append(LocationTraceEventError(index=index, reason=err))
                continue
            assert key is not None
            failure = kernel.transfer(index, key, event.from_location, event.to_location)
        else:
            assert isinstance(event, TracePackingEvent)
            parent_key, err = parse_party(event.parent_scan, "父件", index)
            if err is not None:
                errors.append(LocationTraceEventError(index=index, reason=err))
                continue
            child_key, err = parse_party(event.child_scan, "子件", index)
            if err is not None:
                errors.append(LocationTraceEventError(index=index, reason=err))
                continue
            assert parent_key is not None and child_key is not None
            if event.action == "pack":
                failure = kernel.pack(index, parent_key, child_key)
            else:
                failure = kernel.unpack(index, parent_key, child_key)

        if failure is not None:
            errors.append(
                LocationTraceEventError(index=index, reason=_failure_reason(failure))
            )

    # 仅对命中召回的库存实物输出
    items: list[LocationTraceItem] = []
    for key, parsed in inventory.items():
        hits = match_rules(parsed, request.rules)
        if not hits:
            continue
        items.append(
            LocationTraceItem(
                gtin=key[0],
                serial=key[1],
                locations=list(kernel.nodes[key].locations),
                rule_ids=hits,  # match_rules 已按规则输入顺序返回
            )
        )
    items.sort(key=lambda item: (item.gtin, item.serial))
    errors.sort(key=lambda error: error.index)
    return LocationTraceResponse(items=items, errors=errors)
