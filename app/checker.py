"""扫码核验的共享逻辑:逐条解析扫描串并匹配召回规则。

被 /v1/recall/check 与 /v1/recall/check-signed 复用;
单条扫描串解析失败不影响其余条目。
"""

from __future__ import annotations

from .gs1 import ScanParseError, parse_scan
from .matcher import match_rules
from .models import Rule, ScanErrorBody, ScanFields, ScanResult


def check_scans(scans: list[str], rules: list[Rule]) -> list[ScanResult]:
    """逐条解析扫描串并匹配规则,返回与 scans 一一对应的核验结果。"""
    results: list[ScanResult] = []
    for index, raw in enumerate(scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            results.append(
                ScanResult(
                    index=index,
                    ok=False,
                    error=ScanErrorBody(offset=exc.offset, reason=exc.reason),
                )
            )
            continue
        results.append(
            ScanResult(
                index=index,
                ok=True,
                fields=ScanFields(
                    gtin=parsed.gtin,
                    batch=parsed.batch,
                    serial=parsed.serial,
                    date=parsed.expiry,
                ),
                matches=match_rules(parsed, rules),
            )
        )
    return results
