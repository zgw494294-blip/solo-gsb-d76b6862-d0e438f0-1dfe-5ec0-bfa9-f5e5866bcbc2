"""召回隔离计划:库存扫描命中召回规则后,沿最终包装父链归并到最高层已封签祖先。

处理流程:

1. 已封签扫描串全部解析并按 GTIN + AI 21 序列号去重;非法、缺序列号或重复时
   整个请求返回 422。
2. 库存扫描逐条解析,须含 AI 21;非法或缺序列号按原下标报错并跳过;
   合法扫描的 GTIN + 序列号重复时整个请求返回 422。
3. 装拆操作复用 packing.replay_packing(即 app.replay 重放内核的独立装拆模式)
   重放,失败操作不改变关系,按原下标报错。
4. 合法库存扫描复用 matcher.match_rules 匹配召回规则;命中实物沿最终父链
   向上选最高层已封签祖先(含自身)作为隔离单元,没有已封签祖先时取实物自身。
5. 按单元合并命中库存扫描下标与规则 ID;规则 ID 去重后保持规则输入顺序,
   单元按 GTIN、序列号排序;未命中召回的库存扫描不产出单元。
"""

from __future__ import annotations

from fastapi import HTTPException

from .gs1 import ParsedScan, ScanParseError, parse_scan
from .matcher import match_rules
from .models import (
    IsolationPlanRequest,
    IsolationPlanResponse,
    IsolationScanError,
    IsolationUnit,
)
from .packing import ItemKey, replay_packing


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(status_code=422, detail=reason)


def _item_label(key: ItemKey) -> str:
    return f"实物 (GTIN {key[0]}, 序列号 '{key[1]}')"


def _parse_sealed_scans(raw_scans: list[str]) -> set[ItemKey]:
    """解析已封签扫描串;非法、缺序列号或重复时抛 422。"""
    sealed: set[ItemKey] = set()
    first_seen: dict[ItemKey, int] = {}
    for index, raw in enumerate(raw_scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            raise _unprocessable(f"封签扫描 {index} 非法: {exc.reason}") from exc
        if parsed.serial is None:
            raise _unprocessable(f"封签扫描 {index} 缺少 AI 21 序列号,无法识别实物")
        key = (parsed.gtin, parsed.serial)
        if key in sealed:
            raise _unprocessable(
                f"封签扫描 {index} 与封签扫描 {first_seen[key]} 重复: "
                f"同一{_item_label(key)}"
            )
        sealed.add(key)
        first_seen[key] = index
    return sealed


def _parse_inventory_scans(
    raw_scans: list[str],
) -> tuple[list[tuple[int, ParsedScan]], list[IsolationScanError]]:
    """逐条解析库存扫描;非法或缺序列号按原下标报错并跳过,合法扫描重复时抛 422。"""
    valid: list[tuple[int, ParsedScan]] = []
    errors: list[IsolationScanError] = []
    first_seen: dict[ItemKey, int] = {}
    for index, raw in enumerate(raw_scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            errors.append(IsolationScanError(index=index, reason=f"扫描非法: {exc.reason}"))
            continue
        if parsed.serial is None:
            errors.append(
                IsolationScanError(index=index, reason="扫描串缺少 AI 21 序列号,无法识别实物")
            )
            continue
        key = (parsed.gtin, parsed.serial)
        if key in first_seen:
            raise _unprocessable(
                f"库存扫描 {index} 与库存扫描 {first_seen[key]} 重复: "
                f"同一{_item_label(key)}"
            )
        first_seen[key] = index
        valid.append((index, parsed))
    return valid, errors


def _isolation_unit(
    key: ItemKey, parent_of: dict[ItemKey, ItemKey], sealed: set[ItemKey]
) -> ItemKey:
    """沿最终父链选最高层已封签祖先(含自身);没有已封签祖先时返回实物自身。"""
    unit = key
    node = key
    guard: set[ItemKey] = set()
    while node in parent_of and node not in guard:
        guard.add(node)
        node = parent_of[node]
        if node in sealed:
            unit = node
    return unit


def build_isolation_plan(request: IsolationPlanRequest) -> IsolationPlanResponse:
    """重放装拆操作并匹配召回规则,返回隔离单元与各类失败原因。"""
    sealed = _parse_sealed_scans(request.sealed_scans)
    valid, errors = _parse_inventory_scans(request.scans)

    # 装拆操作重放:失败操作不改变关系,按原下标报错
    parent_of, _known, operation_errors = replay_packing(request.operations)

    # 规则匹配,命中实物归并到所属隔离单元
    unit_scans: dict[ItemKey, list[int]] = {}
    unit_rules: dict[ItemKey, set[str]] = {}
    for index, parsed in valid:
        hits = match_rules(parsed, request.rules)
        if not hits:
            continue
        unit = _isolation_unit((parsed.gtin, parsed.serial), parent_of, sealed)
        unit_scans.setdefault(unit, []).append(index)
        unit_rules.setdefault(unit, set()).update(hits)

    units = [
        IsolationUnit(
            gtin=key[0],
            serial=key[1],
            scan_indices=sorted(unit_scans[key]),
            # 规则 ID 去重后保持规则输入顺序
            rule_ids=[rule.id for rule in request.rules if rule.id in unit_rules[key]],
        )
        for key in sorted(unit_scans)
    ]
    errors.sort(key=lambda error: error.index)
    operation_errors.sort(key=lambda error: error.index)
    return IsolationPlanResponse(
        units=units, errors=errors, operation_errors=operation_errors
    )
