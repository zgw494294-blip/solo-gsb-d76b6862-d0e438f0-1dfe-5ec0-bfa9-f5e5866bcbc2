"""Compact JWS 编解码辅助:无填充 base64url,以及拒绝重复 JSON 键的解析。

三段(头部 / 载荷 / 签名)均使用 RFC 7515 附录 C 的无填充 base64url;
头部与载荷是 JSON,任一层出现重复键都视为格式非法。
"""

from __future__ import annotations

import base64
import json

_B64URL_ALPHABET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class JWSFormatError(ValueError):
    """compact JWS 段编码或 JSON 结构非法。"""


def b64url_encode(data: bytes) -> str:
    """编码为无填充 base64url 字符串。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(segment: str) -> bytes:
    """解码无填充 base64url;空段、填充 ('=')、非字母表字符一律拒绝。"""
    if not segment or any(ch not in _B64URL_ALPHABET for ch in segment):
        raise JWSFormatError(
            "base64url 段为空或含非法字符(仅允许 A-Z a-z 0-9 - _,且不得有 '=' 填充)"
        )
    # 合法无填充 base64url 长度模 4 不会余 1
    if len(segment) % 4 == 1:
        raise JWSFormatError("base64url 段长度非法")
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # 理论上字母表与长度校验后不会发生
        raise JWSFormatError("base64url 解码失败") from exc


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise JWSFormatError(f"JSON 键重复: {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_constant(value: str) -> None:
    raise JWSFormatError(f"非法 JSON 常量: {value}")  # NaN / Infinity / -Infinity


def json_decode_b64url(segment: str) -> object:
    """解码 base64url 段并按 UTF-8 解析 JSON,任意层重复键即拒绝。"""
    raw = b64url_decode(segment)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JWSFormatError("base64url 解码结果不是合法 UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except JWSFormatError:
        raise
    except json.JSONDecodeError as exc:
        raise JWSFormatError(f"JSON 解析失败: {exc.msg}") from exc
