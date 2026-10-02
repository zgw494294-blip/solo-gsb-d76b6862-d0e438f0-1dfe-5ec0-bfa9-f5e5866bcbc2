"""实物交接审计:按时间顺序重放交接事件,整理每件实物的持有人流转结果。

本模块只负责扫描串解析适配、交接链 / 批号 / 有效期校验与结果整理;持有人、成功
事件下标等状态转换统一走 `app.replay.ReplayKernel`(独立交接模式)。批号(AI 10)、
有效期(AI 17)基准取**首次成功事件**(经内核提交前回调比对),失败事件不改变
持有人、不留下成功下标,后续事件继续从失败前状态处理。
"""

from __future__ import annotations

from datetime import timezone

from .gs1 import ScanParseError, parse_scan
from .models import (
    CustodyAuditRequest,
    CustodyAuditResponse,
    CustodyEventError,
    CustodyItem,
)
from .replay import (
    FIRST_TRANSFER_FROM_NOT_NULL,
    HOLDER_MISMATCH,
    PRECHECK_REJECTED,
    ReplayConfig,
    ReplayFailure,
    ReplayKernel,
    ItemKey,
    fmt_value,
)


def _failure_reason(failure: ReplayFailure) -> str:
    """把内核 / 提交前回调的失败结果整理为交接审计接口的既有文案。"""
    if failure.code == FIRST_TRANSFER_FROM_NOT_NULL:
        return f"实物首次事件的 from_location 须为 null,实际为 {failure.actual}"
    if failure.code == HOLDER_MISMATCH:
        return (
            "from_location 须等于上次成功事件的 to_location "
            f"{failure.expected},实际为 {failure.actual}"
        )
    # 批号 / 有效期冲突等提交前回调直接给出文案
    return failure.detail or "交接失败"


def audit_custody(request: CustodyAuditRequest) -> CustodyAuditResponse:
    """重放交接事件,返回每件实物的成功事件与最终持有人,以及失败事件的原因。"""
    order = sorted(
        range(len(request.events)),
        key=lambda i: (request.events[i].occurred_at.astimezone(timezone.utc), i),
    )
    kernel = ReplayKernel(ReplayConfig.custody())
    errors: list[CustodyEventError] = []

    for index in order:
        event = request.events[index]
        try:
            parsed = parse_scan(event.scan)
        except ScanParseError as exc:
            errors.append(CustodyEventError(index=index, reason=f"扫描非法: {exc.reason}"))
            continue
        if parsed.serial is None:
            errors.append(
                CustodyEventError(index=index, reason="扫描串缺少 AI 21 序列号,无法识别实物")
            )
            continue

        key: ItemKey = (parsed.gtin, parsed.serial)
        existing = kernel.get(key)
        first = existing is None or existing.holder is None

        def precheck(
            _kernel: ReplayKernel,
            _key: ItemKey,
            _index: int,
            _first: bool,
            node: object,
        ) -> ReplayFailure | None:
            """结构 / 交接链校验已过、提交前比对批号 / 有效期基准(失败即否决)。"""
            if _first:
                return None
            assert node is not None
            baseline_batch = node.meta["batch"]
            if parsed.batch != baseline_batch:
                return ReplayFailure(
                    code=PRECHECK_REJECTED,
                    index=_index,
                    item=_key,
                    detail=(
                        "批号与首次成功扫描冲突: "
                        f"首次为 {fmt_value(baseline_batch)},"
                        f"本次为 {fmt_value(parsed.batch)}"
                    ),
                )
            baseline_expiry = node.meta["expiry"]
            if parsed.expiry != baseline_expiry:
                return ReplayFailure(
                    code=PRECHECK_REJECTED,
                    index=_index,
                    item=_key,
                    detail=(
                        "有效期与首次成功扫描冲突: "
                        f"首次为 {fmt_value(baseline_expiry)},"
                        f"本次为 {fmt_value(parsed.expiry)}"
                    ),
                )
            return None

        failure = kernel.transfer(
            index, key, event.from_location, event.to_location, precheck
        )
        if failure is not None:
            errors.append(CustodyEventError(index=index, reason=_failure_reason(failure)))
            continue
        if first:
            # 首次成功交接:固化批号 / 有效期基准(含字段缺失)
            kernel.nodes[key].meta.update(batch=parsed.batch, expiry=parsed.expiry)

    items = [
        CustodyItem(
            gtin=key[0],
            serial=key[1],
            events=list(node.success_indices),
            holder=node.holder,
        )
        for key, node in kernel.nodes.items()
    ]
    items.sort(key=lambda item: (item.gtin, item.serial))
    errors.sort(key=lambda error: error.index)
    return CustodyAuditResponse(items=items, errors=errors)
