"""请求 / 响应模型。"""

from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .gs1 import VARIABLE_MAX, gtin_check_ok


def _is_printable(s: str) -> bool:
    return all(" " <= c <= "~" for c in s)


def _ensure_unique_rule_ids(rules: list["Rule"]) -> list["Rule"]:
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise ValueError("规则 ID 必须唯一")
    return rules


class Rule(BaseModel):
    """召回规则:GTIN 必填且须通过模 10 校验;批号 / 序列号 / ISO 日期闭区间可选。

    规则中附带的字段,扫描串里必须存在且相等才算命中。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    gtin: str
    batch: str | None = None
    serial: str | None = None
    date_from: dt_date | None = None
    date_to: dt_date | None = None

    @field_validator("gtin")
    @classmethod
    def _gtin_valid(cls, v: str) -> str:
        if len(v) != 14 or any(not "0" <= c <= "9" for c in v):
            raise ValueError("GTIN 须为 14 位数字")
        if not gtin_check_ok(v):
            raise ValueError("GTIN 校验位错误 (模 10)")
        return v

    @field_validator("batch", "serial")
    @classmethod
    def _variable_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not 1 <= len(v) <= VARIABLE_MAX or not _is_printable(v):
            raise ValueError(f"须为 1~{VARIABLE_MAX} 位可打印 ASCII")
        return v

    @model_validator(mode="after")
    def _date_range_valid(self) -> "Rule":
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from 不得晚于 date_to")
        return self

    @classmethod
    def model_validate_list(cls, raw: list[object]) -> list["Rule"]:
        """校验规则数组(含 GTIN 模 10、字段约束)并保证规则 ID 唯一。"""
        rules = TypeAdapter(list[Rule]).validate_python(raw)
        return _ensure_unique_rule_ids(rules)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scans: list[str] = Field(min_length=1, max_length=100)
    rules: list[Rule] = Field(min_length=1, max_length=100)

    @field_validator("rules")
    @classmethod
    def _rule_ids_unique(cls, v: list[Rule]) -> list[Rule]:
        return _ensure_unique_rule_ids(v)


class ScanFields(BaseModel):
    gtin: str
    batch: str | None = None
    serial: str | None = None
    date: dt_date | None = None  # AI 17 解析结果,ISO 格式


class ScanErrorBody(BaseModel):
    offset: int  # 相对原始扫描串(含 ]d2 前缀),从 0 计
    reason: str


class ScanResult(BaseModel):
    index: int
    ok: bool
    fields: ScanFields | None = None
    matches: list[str] = Field(default_factory=list)
    error: ScanErrorBody | None = None


class CheckResponse(BaseModel):
    results: list[ScanResult]


def _reject_bool(value: object) -> object:
    # bool 是 int 的子类,pydantic 默认会把 true/false 强转成 1/0,显式拒绝
    if isinstance(value, bool):
        raise ValueError("须为整数(Unix 秒),不能为布尔值")
    return value


UnixSeconds = Annotated[int, BeforeValidator(_reject_bool)]


class SignedCheckRequest(BaseModel):
    """/v1/recall/check-signed 请求:现有 scans + compact JWS + 当前 Unix 秒。

    载荷中的 rules、JWS 头部与签名在路由逻辑中校验(分别对应 422 / 403)。
    """

    model_config = ConfigDict(extra="forbid")

    scans: list[str] = Field(min_length=1, max_length=100)
    jws: str = Field(min_length=1, description="compact JWS(header.payload.signature)")
    now: UnixSeconds = Field(description="当前时间,Unix 秒")


class SignedCheckResponse(BaseModel):
    kid: str  # 验签通过的头部 kid
    results: list[ScanResult]  # 与 /v1/recall/check 完全相同的逐条核验结果


# ---------- 实物交接审计 (/v1/custody/audit) ----------


class CustodyEvent(BaseModel):
    """单条实物交接事件:扫描串 + 带时区的发生时间 + 交出方 / 接收方。"""

    model_config = ConfigDict(extra="forbid")

    scan: str
    occurred_at: datetime
    from_location: str | None  # 必填但可为 null;实物首次事件须为 null
    to_location: str = Field(min_length=1)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("occurred_at 须带时区信息")
        return v


class CustodyAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[CustodyEvent] = Field(min_length=1, max_length=300)


class CustodyItem(BaseModel):
    gtin: str
    serial: str
    events: list[int]  # 成功事件的原始下标,按处理顺序(时间升序)排列
    holder: str  # 最终持有人:最后一次成功事件的 to_location


class CustodyEventError(BaseModel):
    index: int  # 失败事件的原始下标
    reason: str


class CustodyAuditResponse(BaseModel):
    items: list[CustodyItem]  # 按 GTIN、序列号排序
    errors: list[CustodyEventError]  # 按原始下标排序


# ---------- 交接日志最少剔除 (/v1/custody/trim) ----------


def _locked_index_strict(value: object) -> object:
    # JSON 数字经 pydantic 宽松模式会把 0.0 这类整数浮点强转为 int;
    # 下标须是真正的整数(布尔值同样拒绝)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("locked_indices 下标须为整数")
    return value


LockedIndex = Annotated[int, BeforeValidator(_locked_index_strict)]


class CustodyTrimRequest(BaseModel):
    """交接日志最少剔除请求:1~300 条现有格式交接事件 + 可选锁定事件原下标。

    locked_indices 重复或越界(相对 events 下标)时整个请求返回 422;
    锁定事件必须剔除(扫描非法 / 缺序列号)或无法同时保留时亦返回 422。
    """

    model_config = ConfigDict(extra="forbid")

    events: list[CustodyEvent] = Field(min_length=1, max_length=300)
    locked_indices: list[LockedIndex] = Field(default_factory=list)

    @model_validator(mode="after")
    def _locked_indices_valid(self) -> "CustodyTrimRequest":
        count = len(self.events)
        seen: set[int] = set()
        for index in self.locked_indices:
            if index < 0 or index >= count:
                raise ValueError(f"locked_indices 下标 {index} 越界: events 共 {count} 条")
            if index in seen:
                raise ValueError(f"locked_indices 下标 {index} 重复")
            seen.add(index)
        return self


class CustodyTrimItem(BaseModel):
    """保留子序列中的一件实物及其最终持有人;未保留任何事件的实物不列出。"""

    gtin: str
    serial: str
    holder: str  # 最终持有人:最后一条保留事件的 to_location


class CustodyTrimResponse(BaseModel):
    kept_indices: list[int]  # 保留事件的原下标,按处理顺序(UTC 升序,同刻输入顺序)
    removed_indices: list[int]  # 剔除事件的原下标,按原下标升序
    items: list[CustodyTrimItem]  # 保留实物(GTIN+序列号)与最终持有人,按 GTIN、序列号排序


# ---------- 装拆包装审计 (/v1/packing/audit) ----------


class PackingOperation(BaseModel):
    """单条装/拆操作:父件与子件均以含 AI 21 的 GS1 扫描串识别。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["pack", "unpack"]
    parent_scan: str
    child_scan: str


class PackingAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[PackingOperation] = Field(min_length=1, max_length=300)


class PackingItemRef(BaseModel):
    """对一件实物(GTIN+序列号)的引用。"""

    gtin: str
    serial: str


class PackingItem(BaseModel):
    """一件出现在成功操作中的实物及其当前包装关系。"""

    gtin: str
    serial: str
    parent: PackingItemRef | None  # 当前直接父件;无父件时为 null
    children: list[PackingItemRef]  # 当前直接子件,按 GTIN、序列号排序
    root: PackingItemRef  # 最外层容器;无父件时为自身


class PackingError(BaseModel):
    index: int  # 失败操作的原始下标
    reason: str


class PackingAuditResponse(BaseModel):
    items: list[PackingItem]  # 成功操作涉及的全部实物,按 GTIN、序列号排序
    errors: list[PackingError]  # 按原始下标排序


# ---------- 召回隔离计划 (/v1/recall/isolation-plan) ----------


class IsolationPlanRequest(BaseModel):
    """隔离计划请求:库存扫描 + 召回规则 + 装拆操作 + 已封签实物扫描串。"""

    model_config = ConfigDict(extra="forbid")

    scans: list[str] = Field(min_length=1, max_length=100)  # 库存扫描,须含 AI 21
    rules: list[Rule] = Field(min_length=1, max_length=100)  # 召回规则,同召回核验
    operations: list[PackingOperation] = Field(min_length=1, max_length=300)  # 装/拆操作
    sealed_scans: list[str] = Field(max_length=300)  # 已封签实物扫描串,须含 AI 21

    @field_validator("rules")
    @classmethod
    def _rule_ids_unique(cls, v: list[Rule]) -> list[Rule]:
        return _ensure_unique_rule_ids(v)


class IsolationScanError(BaseModel):
    index: int  # 失败库存扫描的原始下标
    reason: str


class IsolationUnit(BaseModel):
    """一个隔离单元:命中实物沿最终父链的最高层已封签祖先(含自身);无已封签祖先时为实物自身。"""

    gtin: str
    serial: str
    scan_indices: list[int]  # 归入本单元的命中库存扫描下标,升序
    rule_ids: list[str]  # 命中的规则 ID,去重后按规则输入顺序


class IsolationPlanResponse(BaseModel):
    units: list[IsolationUnit]  # 按 GTIN、序列号排序;未命中召回的库存不产生单元
    errors: list[IsolationScanError]  # 库存扫描错误,按原始下标排序
    operation_errors: list[PackingError]  # 装拆操作错误,按原始下标排序


# ---------- 召回规则关系分析 (/v1/recall/rule-relations) ----------


class RuleRelationsRequest(BaseModel):
    """规则关系分析请求:2~100 条召回规则,格式同召回核验。"""

    model_config = ConfigDict(extra="forbid")

    rules: list[Rule] = Field(min_length=2, max_length=100)

    @field_validator("rules")
    @classmethod
    def _rule_ids_unique(cls, v: list[Rule]) -> list[Rule]:
        return _ensure_unique_rule_ids(v)


class RulePairRelation(BaseModel):
    """一对命中集合有交集的规则:关系类别 + 一条同时命中两者的合法扫描串。"""

    left_index: int  # 输入下标较小者
    right_index: int
    left_id: str
    right_id: str
    relation: Literal[
        "equivalent",  # 等价:命中集合相同
        "left_contains_right",  # 左包含右:右规则命中集是左规则的真子集
        "right_contains_left",  # 右包含左:左规则命中集是右规则的真子集
        "partial_overlap",  # 部分相交:有交集但互不包含
    ]
    witness: str  # 同时命中两条规则的合法 GS1 扫描串


class RuleRelationsResponse(BaseModel):
    impossible_rule_ids: list[str]  # 不可能命中任何合法扫描串的规则 ID,按输入顺序
    pairs: list[RulePairRelation]  # 有交集的规则对,按 (left_index, right_index) 排序


# ---------- 到货核对 (/v1/receiving/reconcile) ----------


PositiveInt = Annotated[int, BeforeValidator(_reject_bool)]


class ExpectedLine(BaseModel):
    """到货期望行:有效 GTIN 必填,正整数数量;可限定批号 / 序列号 / 到期日。

    附带的限定字段,扫描串里必须存在且相等,该扫描才能占用本行名额。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)  # 非空字符串,请求内唯一
    gtin: str
    quantity: PositiveInt = Field(ge=1)  # 正整数;全部行总数不超过 60(请求级校验)
    batch: str | None = None
    serial: str | None = None
    expiry: dt_date | None = None  # ISO 到期日,年限 2000~2099

    @field_validator("gtin")
    @classmethod
    def _gtin_valid(cls, v: str) -> str:
        if len(v) != 14 or any(not "0" <= c <= "9" for c in v):
            raise ValueError("GTIN 须为 14 位数字")
        if not gtin_check_ok(v):
            raise ValueError("GTIN 校验位错误 (模 10)")
        return v

    @field_validator("batch", "serial")
    @classmethod
    def _variable_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not 1 <= len(v) <= VARIABLE_MAX or not _is_printable(v):
            raise ValueError(f"须为 1~{VARIABLE_MAX} 位可打印 ASCII")
        return v

    @field_validator("expiry")
    @classmethod
    def _expiry_range_valid(cls, v: dt_date | None) -> dt_date | None:
        if v is not None and not (dt_date(2000, 1, 1) <= v <= dt_date(2099, 12, 31)):
            raise ValueError("到期日须为 2000~2099 年的 ISO 日期")
        return v


def _ensure_unique_line_ids(lines: list[ExpectedLine]) -> list[ExpectedLine]:
    ids = [line.id for line in lines]
    if len(set(ids)) != len(ids):
        raise ValueError("期望行 ID 必须唯一")
    return lines


class ReceivingReconcileRequest(BaseModel):
    """到货核对请求:1~60 条 GS1 扫描串 + 1~60 条期望行(数量总数 ≤ 60)。"""

    model_config = ConfigDict(extra="forbid")

    scans: list[str] = Field(min_length=1, max_length=60)
    lines: list[ExpectedLine] = Field(min_length=1, max_length=60)

    @field_validator("lines")
    @classmethod
    def _lines_valid(cls, v: list[ExpectedLine]) -> list[ExpectedLine]:
        _ensure_unique_line_ids(v)
        if sum(line.quantity for line in v) > 60:
            raise ValueError("全部期望行 quantity 总数不得超过 60")
        return v


class ReceivingScanError(BaseModel):
    index: int  # 解析失败扫描的原始下标
    offset: int  # 相对原始扫描串(含 ]d2 前缀),从 0 计
    reason: str


class ReceivingAssignment(BaseModel):
    index: int  # 扫描原下标
    line_id: str | None  # 分配到的期望行 ID;未分配为 null


class ReceivingLineRemaining(BaseModel):
    id: str  # 期望行 ID
    remaining: int  # 剩余未核销数量


class ReceivingReconcileResponse(BaseModel):
    assignments: list[ReceivingAssignment]  # 与 scans 等长且同序;解析失败 / 重复 / 未分配为 null
    errors: list[ReceivingScanError]  # 解析失败扫描,按原始下标排序
    unassigned_indices: list[int]  # 未分配的有效(且非重复)扫描下标
    duplicate_indices: list[int]  # 同 GTIN+AI21 序列号的后续重复扫描下标
    lines: list[ReceivingLineRemaining]  # 各期望行剩余数量,保持输入顺序


# ---------- 召回实物保管地点追溯 (/v1/recall/location-trace) ----------


class _TraceEventBase(BaseModel):
    """交接 / 装拆事件的公共字段:occurred_at 须为带时区的 ISO 8601 时间。"""

    model_config = ConfigDict(extra="forbid")

    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("occurred_at 须带时区信息")
        return v


class TraceTransferEvent(_TraceEventBase):
    """交接事件:仅作用于无父件实物;首次 from_location 为 null,此后须等于当前持有人。"""

    action: Literal["transfer"]
    scan: str
    from_location: str | None  # 必填但可为 null;实物首次成功交接须为 null
    to_location: str = Field(min_length=1)


class TracePackingEvent(_TraceEventBase):
    """装入 / 取出事件:父件、子件均以含 AI 21 的 GS1 扫描串识别。"""

    action: Literal["pack", "unpack"]
    parent_scan: str
    child_scan: str


# 以 action 为判别字段的联合;非法 action / 字段缺失或多余均由 pydantic 返回 422
TraceEvent = Annotated[
    TraceTransferEvent | TracePackingEvent, Field(discriminator="action")
]


class LocationTraceRequest(BaseModel):
    """地点追溯请求:1~100 条库存扫描 + 1~100 条召回规则 + 1~200 条交接 / 装拆事件。

    库存扫描须全部合法、含 AI 21 且 GTIN+序列号唯一,否则整个请求返回 422;
    事件扫描的合法性、库存引用与批号 / 有效期一致性按事件原下标报错,不生效。
    """

    model_config = ConfigDict(extra="forbid")

    scans: list[str] = Field(min_length=1, max_length=100)  # 库存扫描,须含 AI 21
    rules: list[Rule] = Field(min_length=1, max_length=100)  # 召回规则,同召回核验
    events: list[TraceEvent] = Field(min_length=1, max_length=200)  # 交接 / 装拆事件

    @field_validator("rules")
    @classmethod
    def _rule_ids_unique(cls, v: list[Rule]) -> list[Rule]:
        return _ensure_unique_rule_ids(v)


class LocationTraceItem(BaseModel):
    """一件命中召回的库存实物及其保管地点(按首次到达顺序去重)。"""

    gtin: str
    serial: str
    locations: list[str]  # 按首次到达顺序去重的保管地点(含祖先容器交接带入的地点)
    rule_ids: list[str]  # 命中的规则 ID,按规则输入顺序


class LocationTraceEventError(BaseModel):
    index: int  # 失败事件的原始下标
    reason: str


class LocationTraceResponse(BaseModel):
    items: list[LocationTraceItem]  # 仅含命中召回的库存实物,按 GTIN、序列号排序
    errors: list[LocationTraceEventError]  # 事件错误,按原始下标排序
