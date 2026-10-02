"""扫描结果与召回规则的匹配。"""

from __future__ import annotations

from .gs1 import ParsedScan
from .models import Rule


def match_rules(parsed: ParsedScan, rules: list[Rule]) -> list[str]:
    """按规则顺序返回命中的规则 ID。

    规则所附字段(批号 / 序列号 / 日期区间)须在扫描串中存在并匹配;
    日期区间为闭区间,扫描串须含 AI 17 且解析日期落在区间内。
    """
    hits: list[str] = []
    for rule in rules:
        if rule.gtin != parsed.gtin:
            continue
        if rule.batch is not None and parsed.batch != rule.batch:
            continue
        if rule.serial is not None and parsed.serial != rule.serial:
            continue
        if rule.date_from is not None or rule.date_to is not None:
            if parsed.expiry is None:
                continue
            if rule.date_from is not None and parsed.expiry < rule.date_from:
                continue
            if rule.date_to is not None and parsed.expiry > rule.date_to:
                continue
        hits.append(rule.id)
    return hits
