"""GS1 扫描串解析。

支持的 AI(可按任意顺序出现,每种至多一次):
  01  GTIN,14 位数字,定长,模 10 校验位,必填
  17  日期,6 位数字 YYMMDD,定长(年按 2000~2099 解释,日 00 表示当月末日)
  10  批号,1~20 位可打印 ASCII,变长,仅由 FNC1 或串尾结束
  21  序列号,1~20 位可打印 ASCII,变长,仅由 FNC1 或串尾结束

FNC1 为 ASCII 29 (GS)。扫描串可带符号标识符前缀 "]d2"。
解析失败抛出 ScanParseError,携带相对原始扫描串的偏移(从 0 计)与原因。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

FNC1 = "\x1d"  # ASCII 29 (GS),字段分隔符
SYMBOL_ID = "]d2"  # GS1 DataMatrix 符号标识符前缀

FIXED_LENGTH = {"01": 14, "17": 6}
VARIABLE_AIS = {"10", "21"}
VARIABLE_MAX = 20


class ScanParseError(Exception):
    """单条扫描串解析失败,携带出错偏移与原因。"""

    def __init__(self, offset: int, reason: str) -> None:
        super().__init__(reason)
        self.offset = offset
        self.reason = reason


@dataclass
class ParsedScan:
    gtin: str
    batch: str | None
    serial: str | None
    expiry: date | None


def _is_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def _is_printable(ch: str) -> bool:
    return " " <= ch <= "~"  # 0x20~0x7E


def gtin_check_ok(gtin: str) -> bool:
    """GS1 模 10 校验位验证(14 位 GTIN)。"""
    if len(gtin) != 14 or any(not _is_digit(c) for c in gtin):
        return False
    total = 0
    for i, ch in enumerate(reversed(gtin[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == int(gtin[-1])


def _parse_expiry(raw: str, offset: int) -> date:
    """raw 为 6 位 YYMMDD;offset 为字段值在原始扫描串中的起始偏移。"""
    year = 2000 + int(raw[0:2])
    month = int(raw[2:4])
    day = int(raw[4:6])
    if not 1 <= month <= 12:
        raise ScanParseError(offset + 2, f"AI 17 月份非法: '{raw[2:4]}'")
    last_day = calendar.monthrange(year, month)[1]
    if day == 0:
        day = last_day  # 日 00 取当月末日
    elif day > last_day:
        raise ScanParseError(offset + 4, f"AI 17 日期非法: {year:04d}-{month:02d} 没有 {day:02d} 日")
    return date(year, month, day)


def parse_scan(raw: str) -> ParsedScan:
    """解析单条扫描串,失败抛出 ScanParseError。"""
    base = 0
    data = raw
    if data.startswith(SYMBOL_ID):
        base = len(SYMBOL_ID)
        data = data[base:]
    n = len(data)
    if n == 0:
        raise ScanParseError(base, "扫描串为空")

    seen: dict[str, tuple[str, int]] = {}  # ai -> (value, 字段值起始偏移)
    i = 0
    while i < n:
        ch = data[i]
        if ch == FNC1:
            raise ScanParseError(base + i, "多余的分隔符 FNC1 (ASCII 29)")
        if not _is_digit(ch):
            raise ScanParseError(base + i, f"非法字符 {ch!r}: 此处应为两位数字 AI")
        if i + 1 >= n:
            raise ScanParseError(base + i, "AI 标识被截断: 应为两位数字")
        if not _is_digit(data[i + 1]):
            raise ScanParseError(base + i, f"非法 AI 标识 {data[i:i + 2]!r}: 应为两位数字")
        ai = data[i:i + 2]
        if ai not in FIXED_LENGTH and ai not in VARIABLE_AIS:
            raise ScanParseError(base + i, f"未知 AI: '{ai}'")
        if ai in seen:
            raise ScanParseError(base + i, f"AI '{ai}' 重复出现")
        i += 2
        value_offset = base + i

        chars: list[str] = []
        if ai in FIXED_LENGTH:
            length = FIXED_LENGTH[ai]
            for _ in range(length):
                if i >= n:
                    raise ScanParseError(base + i, f"AI '{ai}' 长度不足: 需要 {length} 位数字")
                c = data[i]
                if c == FNC1:
                    raise ScanParseError(base + i, f"AI '{ai}' 长度不足: 提前遇到分隔符,需要 {length} 位数字")
                if not _is_digit(c):
                    raise ScanParseError(base + i, f"AI '{ai}' 含非法字符 {c!r}: 应为数字")
                chars.append(c)
                i += 1
        else:
            while i < n and data[i] != FNC1:
                c = data[i]
                if not _is_printable(c):
                    raise ScanParseError(base + i, f"AI '{ai}' 含非法字符: 0x{ord(c):02X} 非可打印 ASCII")
                if len(chars) >= VARIABLE_MAX:
                    raise ScanParseError(base + i, f"AI '{ai}' 超过最大长度 {VARIABLE_MAX}")
                chars.append(c)
                i += 1
            if not chars:
                raise ScanParseError(value_offset, f"AI '{ai}' 值为空: 长度须为 1~{VARIABLE_MAX}")

        seen[ai] = ("".join(chars), value_offset)

        # 字段后允许一个 FNC1 分隔符(定长字段后可省略)
        if i < n and data[i] == FNC1:
            i += 1

    if "01" not in seen:
        raise ScanParseError(base + n, "缺少必填 AI '01' (GTIN)")

    gtin, gtin_value_offset = seen["01"]
    if not gtin_check_ok(gtin):
        raise ScanParseError(gtin_value_offset + 13, "GTIN 校验位错误 (模 10)")

    expiry = None
    if "17" in seen:
        raw_date, date_value_offset = seen["17"]
        expiry = _parse_expiry(raw_date, date_value_offset)

    return ParsedScan(
        gtin=gtin,
        batch=seen["10"][0] if "10" in seen else None,
        serial=seen["21"][0] if "21" in seen else None,
        expiry=expiry,
    )
