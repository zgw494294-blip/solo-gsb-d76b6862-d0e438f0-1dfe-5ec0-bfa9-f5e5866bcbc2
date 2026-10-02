"""本地信任文件:kid -> Ed25519 公钥与授权 GTIN 前缀。

信任文件为 JSON(路径由环境变量 RECALL_TRUST_FILE 指定,默认 trust/recall-keys.json),
形如:

{
  "keys": [
    {
      "kid": "recall-key-001",
      "public_key": "BASE64URL(32 字节 Ed25519 公钥)",
      "gtin_prefixes": ["0950600", "0859"]
    }
  ]
}

公钥只从本地信任文件读取,不联网取钥。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .jws import b64url_decode

DEFAULT_TRUST_FILE = os.environ.get(
    "RECALL_TRUST_FILE", "trust/recall-keys.json"
)


class TrustStoreError(RuntimeError):
    """信任文件缺失或内容非法(服务端配置问题)。"""


class UnknownKeyError(LookupError):
    """信任文件中没有该 kid。"""


@dataclass(frozen=True)
class TrustedKey:
    kid: str
    public_key: Ed25519PublicKey
    gtin_prefixes: tuple[str, ...]


def _load_trust_file(path: str) -> dict[str, TrustedKey]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise TrustStoreError(f"信任文件不存在: {path}") from exc
    except OSError as exc:
        raise TrustStoreError(f"信任文件无法读取: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TrustStoreError(f"信任文件不是合法 JSON: {path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise TrustStoreError("信任文件结构非法: 顶层须为含 'keys' 数组的对象")

    store: dict[str, TrustedKey] = {}
    for index, entry in enumerate(data["keys"]):
        if not isinstance(entry, dict):
            raise TrustStoreError(f"信任文件 keys[{index}] 不是对象")
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TrustStoreError(f"信任文件 keys[{index}] 缺少非空字符串 'kid'")
        if kid in store:
            raise TrustStoreError(f"信任文件中 kid 重复: {kid!r}")
        raw_key = entry.get("public_key")
        if not isinstance(raw_key, str):
            raise TrustStoreError(f"信任文件 keys[{index}] 缺少字符串 'public_key'")
        try:
            key_bytes = b64url_decode(raw_key)
            public_key = Ed25519PublicKey.from_public_bytes(key_bytes)
        except Exception as exc:
            raise TrustStoreError(
                f"信任文件 keys[{index}] 的 public_key 不是 Ed25519 公钥"
                "(须为 32 字节公钥的无填充 base64url 编码)"
            ) from exc
        # 规范化为 32 字节裸公钥编码,确保加载的就是 Ed25519 公钥本体
        public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

        prefixes = entry.get("gtin_prefixes")
        if not isinstance(prefixes, list) or not prefixes:
            raise TrustStoreError(
                f"信任文件 keys[{index}] 缺少非空数组 'gtin_prefixes'"
            )
        clean_prefixes: list[str] = []
        for prefix in prefixes:
            if (
                not isinstance(prefix, str)
                or not 1 <= len(prefix) <= 14
                or any(not "0" <= c <= "9" for c in prefix)
            ):
                raise TrustStoreError(
                    f"信任文件 keys[{index}] 的 GTIN 前缀非法: {prefix!r}"
                    "(须为 1~14 位数字)"
                )
            clean_prefixes.append(prefix)
        store[kid] = TrustedKey(
            kid=kid, public_key=public_key, gtin_prefixes=tuple(clean_prefixes)
        )

    if not store:
        raise TrustStoreError("信任文件中没有任何受信任密钥")
    return store


def load_trusted_key(kid: str, path: str = DEFAULT_TRUST_FILE) -> TrustedKey:
    """按 kid 从本地信任文件查取公钥与授权 GTIN 前缀;未知 kid 抛 UnknownKeyError。"""
    store = _load_trust_file(path)
    try:
        return store[kid]
    except KeyError as exc:
        raise UnknownKeyError(f"未知密钥 kid: {kid!r}") from exc
