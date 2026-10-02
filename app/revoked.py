"""已签名召回规则包的单包撤销名单(本地 JSON)。

名单文件路径由环境变量 RECALL_REVOKED_FILE 指定(见 signed.py 读取的
DEFAULT_REVOKED_FILE);未设置或为空时不启用单包撤销,check-signed 保持旧行为。

名单为 JSON,仅含一个 "revoked" 数组,元素为 64 位小写十六进制 SHA-256 摘要,
允许重复(重复元素等价于一条):

{
  "revoked": [
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
  ]
}

包标识取 JWS 原始前两段以点连接的 ASCII 字节的 SHA-256(即 Ed25519 的
签名输入),与第三段签名无关,因此签名段编码变化不影响标识。单包撤销只命中
该包本身,不连带同一 kid 的其他包。

文件不存在 / 不可读 / JSON 非法 / 结构或元素非法一律视为服务端配置错误,
调用方须返回 500,不能沿用任何旧名单放行。

每次调用 load_revoked_digests 都重新打开并读盘,不做进程内缓存:宿主机
原子替换(os.replace / rename(2))文件后,下一次请求即读到新内容,
无需重启进程或容器。
"""

from __future__ import annotations

import hashlib
import json
import os
import re

DEFAULT_REVOKED_FILE = os.environ.get("RECALL_REVOKED_FILE", "")

_HEX64_RE = re.compile(r"[0-9a-f]{64}")


class RevocationListError(RuntimeError):
    """撤销名单缺失 / 不可读 / 结构或元素非法(服务端配置问题)。"""


def package_digest(header_seg: str, payload_seg: str) -> str:
    """计算包标识:SHA-256(header.payload 原始两段 ASCII 字节)的十六进制摘要。

    与 Ed25519 签名输入完全一致;只取前两段,签名段如何编码都不影响结果。
    """
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    return hashlib.sha256(signing_input).hexdigest()


def load_revoked_digests(path: str = DEFAULT_REVOKED_FILE) -> frozenset[str]:
    """读取并校验撤销名单,返回摘要集合;每次调用都重新读盘。

    未配置路径或文件 / 内容非法时抛 RevocationListError,由调用方按 500 处理
    (失败即拒绝,不回退到旧名单)。
    """
    if not path:
        raise RevocationListError("撤销名单路径未配置")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise RevocationListError(f"撤销名单不存在: {path}") from exc
    except OSError as exc:  # 权限不足、路径是目录等
        raise RevocationListError(f"撤销名单无法读取: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RevocationListError(f"撤销名单不是合法 JSON: {path}: {exc}") from exc

    if not isinstance(data, dict) or set(data.keys()) != {"revoked"}:
        raise RevocationListError("撤销名单结构非法: 顶层须为仅含 'revoked' 数组的对象")
    raw_digests = data["revoked"]
    if not isinstance(raw_digests, list):
        raise RevocationListError("撤销名单结构非法: 'revoked' 须为数组")

    digests: set[str] = set()
    for index, entry in enumerate(raw_digests):
        if not isinstance(entry, str) or _HEX64_RE.fullmatch(entry) is None:
            raise RevocationListError(
                f"撤销名单 revoked[{index}] 非法: 须为 64 位小写十六进制 SHA-256 摘要"
            )
        # 允许重复:集合天然去重,重复条目与单条等价
        digests.add(entry)
    return frozenset(digests)
