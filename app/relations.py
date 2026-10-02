"""召回规则关系分析:上线前比较规则命中集合,找出可能命中同一扫描串的规则对。

规则的命中集合按字段维度(GTIN / 批号 / 序列号 / 日期区间)描述,匹配语义与
matcher.match_rules 一致:

- GTIN 相等是两条规则有交集的前提;
- 批号 / 序列号:规则附带时扫描串须含对应 AI 且相等,未附带时不约束;
- 日期区间:规则附带时扫描串须含 AI 17 且落在闭区间内。AI 17 只能表达
  2000~2099 年的合法日期,规则区间先夹取到该范围;夹取后为空的规则
  不可能命中任何合法扫描串,按输入顺序列入 impossible_rule_ids。

其余规则两两比较,仅报告命中集合有交集的规则对:等价 / 左包含右(右规则命中集
是左规则的真子集)/ 右包含左 / 部分相交,并附一条同时命中两者的合法 GS1
扫描串作为证据(批号、序列号只在约束需要时出现;日期取交集内最早一天;
变长字段后仍有 AI 时加 FNC1)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .gs1 import FNC1
from .models import (
    Rule,
    RulePairRelation,
    RuleRelationsRequest,
    RuleRelationsResponse,
)

# AI 17 年份按 2000~2099 解释,合法扫描串的日期必落在该闭区间内
SCAN_MIN_DATE = date(2000, 1, 1)
SCAN_MAX_DATE = date(2099, 12, 31)

# 单维度比较结果
_EQUAL = "equal"  # 两规则在该维度上的允许集合相同
_A_SUB = "a_sub"  # 左规则在该维度上的集合是右规则的真子集
_B_SUB = "b_sub"  # 右规则在该维度上的集合是左规则的真子集
_PARTIAL = "partial"  # 有交集但互不包含
_DISJOINT = "disjoint"  # 无交集


@dataclass(frozen=True)
class _RuleSpace:
    """一条规则命中的字段组合空间(日期已夹取到 AI 17 可表达的区间)。"""

    gtin: str
    batch: str | None  # None 表示不约束
    serial: str | None
    dates: tuple[date, date] | None  # None 表示不约束;否则为闭区间 [lo, hi]


def _space_of(rule: Rule) -> _RuleSpace | None:
    """计算规则的命中空间;不可能命中任何合法扫描串时返回 None。"""
    dates: tuple[date, date] | None = None
    if rule.date_from is not None or rule.date_to is not None:
        lo = rule.date_from if rule.date_from is not None else SCAN_MIN_DATE
        hi = rule.date_to if rule.date_to is not None else SCAN_MAX_DATE
        lo = max(lo, SCAN_MIN_DATE)
        hi = min(hi, SCAN_MAX_DATE)
        if lo > hi:  # 与 AI 17 可表达的日期区间(2000~2099)无交集
            return None
        dates = (lo, hi)
    return _RuleSpace(gtin=rule.gtin, batch=rule.batch, serial=rule.serial, dates=dates)


def _compare_optional(a: str | None, b: str | None) -> str:
    """比较批号 / 序列号维度:附带即约束为该值,未附带即不约束。"""
    if a is None and b is None:
        return _EQUAL
    if a is not None and b is not None:
        return _EQUAL if a == b else _DISJOINT
    return _A_SUB if a is not None else _B_SUB


def _compare_dates(
    a: tuple[date, date] | None, b: tuple[date, date] | None
) -> str:
    """比较日期维度:附带即约束为闭区间(且扫描串须含 AI 17),未附带即不约束。"""
    if a is None and b is None:
        return _EQUAL
    if a is None:
        return _B_SUB
    if b is None:
        return _A_SUB
    (a_lo, a_hi), (b_lo, b_hi) = a, b
    if a_hi < b_lo or b_hi < a_lo:
        return _DISJOINT
    if a_lo == b_lo and a_hi == b_hi:
        return _EQUAL
    if b_lo <= a_lo and a_hi <= b_hi:
        return _A_SUB
    if a_lo <= b_lo and b_hi <= a_hi:
        return _B_SUB
    return _PARTIAL


def _combine(dims: list[str]) -> str | None:
    """汇总各维度比较结果;命中集合无交集时返回 None。"""
    if _DISJOINT in dims:
        return None
    if _PARTIAL in dims:
        return "partial_overlap"
    a_sub = _A_SUB in dims
    b_sub = _B_SUB in dims
    if a_sub and b_sub:
        return "partial_overlap"
    if a_sub:  # 左规则命中集是右规则的真子集
        return "right_contains_left"
    if b_sub:  # 右规则命中集是左规则的真子集
        return "left_contains_right"
    return "equivalent"


def _relate(a: _RuleSpace, b: _RuleSpace) -> str | None:
    """比较两条规则的命中集合;无交集返回 None。"""
    if a.gtin != b.gtin:
        return None
    return _combine(
        [
            _compare_optional(a.batch, b.batch),
            _compare_optional(a.serial, b.serial),
            _compare_dates(a.dates, b.dates),
        ]
    )


def _intersect_dates(
    a: tuple[date, date] | None, b: tuple[date, date] | None
) -> tuple[date, date] | None:
    """两规则的日期约束求交;均无约束时返回 None(证据串不必含 AI 17)。"""
    if a is None and b is None:
        return None
    lo, hi = a if a is not None else (SCAN_MIN_DATE, SCAN_MAX_DATE)
    if b is not None:
        lo = max(lo, b[0])
        hi = min(hi, b[1])
    return (lo, hi)  # 调用前已确认两规则有交集,lo <= hi


def _witness_scan(a: _RuleSpace, b: _RuleSpace) -> str:
    """构造同时命中两条规则的一条合法 GS1 扫描串。

    批号 / 序列号只在约束需要时出现;日期取交集内最早一天;
    变长字段(AI 10/21)后仍有 AI 时加 FNC1 分隔。
    """
    segments: list[tuple[bool, str]] = []  # (是否变长字段, 片段)
    date_range = _intersect_dates(a.dates, b.dates)
    if date_range is not None:
        earliest = date_range[0]
        yymmdd = f"{earliest.year % 100:02d}{earliest.month:02d}{earliest.day:02d}"
        segments.append((False, "17" + yymmdd))
    batch = a.batch if a.batch is not None else b.batch
    if batch is not None:
        segments.append((True, "10" + batch))
    serial = a.serial if a.serial is not None else b.serial
    if serial is not None:
        segments.append((True, "21" + serial))

    scan = "01" + a.gtin
    for index, (variable, segment) in enumerate(segments):
        scan += segment
        if variable and index + 1 < len(segments):
            scan += FNC1
    return scan


def analyze_rule_relations(request: RuleRelationsRequest) -> RuleRelationsResponse:
    """列出不可能命中的规则,并两两比较其余规则,返回有交集的规则对。"""
    rules = request.rules
    spaces = [_space_of(rule) for rule in rules]
    impossible = [rule.id for rule, space in zip(rules, spaces) if space is None]

    pairs: list[RulePairRelation] = []
    for i, left in enumerate(rules):
        if spaces[i] is None:
            continue
        for j in range(i + 1, len(rules)):
            if spaces[j] is None:
                continue
            relation = _relate(spaces[i], spaces[j])
            if relation is None:
                continue
            pairs.append(
                RulePairRelation(
                    left_index=i,
                    right_index=j,
                    left_id=left.id,
                    right_id=rules[j].id,
                    relation=relation,
                    witness=_witness_scan(spaces[i], spaces[j]),
                )
            )
    return RuleRelationsResponse(impossible_rule_ids=impossible, pairs=pairs)
