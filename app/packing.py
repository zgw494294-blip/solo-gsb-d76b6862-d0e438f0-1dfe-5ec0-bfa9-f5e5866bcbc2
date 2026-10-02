"""装拆包装审计:重放装入(pack)/取出(unpack)操作,整理包装关系结果。

本模块只负责两端扫描串的解析适配、失败原因文案与结果整理;装 / 拆的状态转换
统一走 `app.replay.ReplayKernel`(独立装拆模式:不要求库存或持有人,父件随成功
装入登记,失败操作不会把实物加入成功涉及集合)。召回隔离计划复用同一内核入口
(`replay_packing`)。
"""

from __future__ import annotations

from .gs1 import ScanParseError, parse_scan
from .models import (
    PackingAuditRequest,
    PackingAuditResponse,
    PackingError,
    PackingItem,
    PackingItemRef,
    PackingOperation,
)
from .replay import (
    CHILD_HAS_PARENT,
    NOT_DIRECT_PARENT,
    PACK_CYCLE,
    ReplayConfig,
    ReplayFailure,
    ReplayKernel,
    SAME_ITEM,
    UNKNOWN_CHILD,
    UNKNOWN_PARENT,
    ItemKey,
    item_label,
)

# 向后兼容:历史代码以 packing.ItemKey 引用实物标识类型
__all__ = [
    "ItemKey",
    "audit_packing",
    "replay_packing",
]


def _ref(key: ItemKey) -> PackingItemRef:
    return PackingItemRef(gtin=key[0], serial=key[1])


def _parse_item(raw_scan: str, role: str, index: int) -> tuple[ItemKey | None, str | None]:
    """解析一端扫描串;失败时返回 (None, 含原下标与角色的原因)。"""
    try:
        parsed = parse_scan(raw_scan)
    except ScanParseError as exc:
        return None, f"操作 {index} 的{role}扫描非法: {exc.reason}"
    if parsed.serial is None:
        return None, f"操作 {index} 的{role}扫描串缺少 AI 21 序列号,无法识别实物"
    return (parsed.gtin, parsed.serial), None


def _failure_reason(failure: ReplayFailure) -> str:
    """把内核失败结果整理为装拆审计接口的既有文案。"""
    code = failure.code
    if code == CHILD_HAS_PARENT:
        assert failure.child is not None and failure.parent is not None
        return (
            f"装入失败:子件{item_label(failure.child)}已有直接父件"
            f"{item_label(failure.parent)},不能重复装入"
        )
    if code == SAME_ITEM:
        return "装入失败:子件不能与父件为同一实物"
    if code == PACK_CYCLE:
        assert failure.child is not None and failure.parent is not None
        return (
            f"装入失败:子件{item_label(failure.child)}是父件{item_label(failure.parent)}"
            "的祖先,装入将形成循环包装"
        )
    if code == UNKNOWN_PARENT:
        assert failure.parent is not None
        return f"取出失败:指定的父件{item_label(failure.parent)}此前未出现在任何成功操作中"
    if code == UNKNOWN_CHILD:
        assert failure.child is not None
        return f"取出失败:指定的子件{item_label(failure.child)}此前未出现在任何成功操作中"
    if code == NOT_DIRECT_PARENT:
        assert failure.child is not None and failure.parent is not None
        current = failure.item
        if current is None:
            actual = "子件当前无直接父件"
        else:
            actual = f"子件的当前直接父件为{item_label(current)}"
        return f"取出失败:{actual},并非指定的父件{item_label(failure.parent)}"
    return failure.detail or "操作失败"


def replay_packing(
    operations: list[PackingOperation],
) -> tuple[dict[ItemKey, ItemKey], set[ItemKey], list[PackingError]]:
    """按输入顺序重放装/拆操作,返回 (最终父子边, 成功操作涉及的实物, 失败原因)。

    包装内核的独立装拆模式:失败操作不改变任何关系,按原下标记录原因;只有成功
    操作涉及的实物才计入 involved。供装拆包装审计与召回隔离计划复用。
    """
    kernel = ReplayKernel(ReplayConfig.packing())
    errors: list[PackingError] = []

    for index, op in enumerate(operations):
        parent_key, err = _parse_item(op.parent_scan, "父件", index)
        if err is not None:
            errors.append(PackingError(index=index, reason=err))
            continue
        child_key, err = _parse_item(op.child_scan, "子件", index)
        if err is not None:
            errors.append(PackingError(index=index, reason=err))
            continue
        assert parent_key is not None and child_key is not None

        if op.action == "pack":
            failure = kernel.pack(index, parent_key, child_key)
        else:
            failure = kernel.unpack(index, parent_key, child_key)
        if failure is not None:
            errors.append(PackingError(index=index, reason=_failure_reason(failure)))

    return kernel.parent_of, kernel.involved, errors


def audit_packing(request: PackingAuditRequest) -> PackingAuditResponse:
    """按输入顺序重放装/拆操作,返回成功操作涉及实物的当前包装关系与失败原因。"""
    parent_of, known, errors = replay_packing(request.operations)

    # 反向收集每个父件的当前直接子件
    children_of: dict[ItemKey, list[ItemKey]] = {}
    for child, parent in parent_of.items():
        children_of.setdefault(parent, []).append(child)

    items = [
        PackingItem(
            gtin=key[0],
            serial=key[1],
            parent=_ref(parent_of[key]) if key in parent_of else None,
            children=[_ref(c) for c in sorted(children_of.get(key, []))],
            root=_ref(_root_of(key, parent_of)),
        )
        for key in known
    ]
    items.sort(key=lambda item: (item.gtin, item.serial))
    errors.sort(key=lambda error: error.index)
    return PackingAuditResponse(items=items, errors=errors)


def _root_of(key: ItemKey, parent_of: dict[ItemKey, ItemKey]) -> ItemKey:
    """沿父链走到最外层容器。"""
    node = key
    guard: set[ItemKey] = set()
    while node in parent_of and node not in guard:
        guard.add(node)
        node = parent_of[node]
    return node
