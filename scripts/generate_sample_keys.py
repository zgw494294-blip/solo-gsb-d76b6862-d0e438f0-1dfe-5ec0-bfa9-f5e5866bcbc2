#!/usr/bin/env python3
"""本地样例生成脚本:生成 Ed25519 密钥、信任文件与 check-signed 请求样例。

用途:本地联调 / Docker Compose 单命令启动的演示数据。**勿用于生产** ——
脚本会把私钥一并写在样例私钥文件里。

用法(在仓库根目录):

    python3 scripts/generate_sample_keys.py

生成产物:

    trust/recall-keys.json             # 信任文件:kid + 公钥 + 授权 GTIN 前缀
    examples/sample-private-keys.json  # 样例私钥(仅本脚本/本地联调用,服务不读取)
    examples/sample-check-signed.json  # POST /v1/recall/check-signed 请求体

常用选项:

    --kid recall-key-001           信任文件中的 kid
    --prefix 0950600               授权 GTIN 前缀(可重复给出多个,1~14 位数字)
    --now 1790000000               请求体中的 now(Unix 秒),默认当前时间
    --nbf-offset -300              nbf = now + nbf_offset(秒)
    --exp-offset 86400             exp = now + exp_offset(秒)
    --gtin 09506001123454          样例规则 / 扫描串所用 14 位 GTIN(须以授权前缀开头)
    --trust-file PATH / --private-file PATH / --request-file PATH
    --force                        覆盖已存在的文件
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_KID = "recall-key-001"
DEFAULT_PREFIX = "0950600"
DEFAULT_GTIN = "09506001123454"  # make_gtin("0950600112345")
FNC1 = "\x1d"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def public_raw(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def private_raw(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def gtin_check_ok(gtin: str) -> bool:
    total = sum(
        int(ch) * (3 if i % 2 == 0 else 1)
        for i, ch in enumerate(reversed(gtin[:-1]))
    )
    return (10 - total % 10) % 10 == int(gtin[-1])


def write_file(path: str, content: str, force: bool) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path) and not force:
        sys.exit(f"目标已存在: {path}(加 --force 覆盖)")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def compact_jws(private_key: Ed25519PrivateKey, kid: str, payload: dict) -> str:
    header = {"alg": "EdDSA", "kid": kid}
    header_seg = b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    payload_seg = b64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    signature = private_key.sign(signing_input)
    return f"{header_seg}.{payload_seg}.{b64url(signature)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kid", default=DEFAULT_KID)
    parser.add_argument(
        "--prefix",
        dest="prefixes",
        action="append",
        help="授权 GTIN 前缀(1~14 位数字),可重复;默认 0950600",
    )
    parser.add_argument("--gtin", default=DEFAULT_GTIN, help="样例 14 位 GTIN")
    parser.add_argument("--now", type=int, default=None, help="Unix 秒;默认当前时间")
    parser.add_argument("--nbf-offset", type=int, default=-300)
    parser.add_argument("--exp-offset", type=int, default=86400)
    parser.add_argument(
        "--trust-file", default=os.path.join(REPO_ROOT, "trust", "recall-keys.json")
    )
    parser.add_argument(
        "--private-file",
        default=os.path.join(REPO_ROOT, "examples", "sample-private-keys.json"),
    )
    parser.add_argument(
        "--request-file",
        default=os.path.join(REPO_ROOT, "examples", "sample-check-signed.json"),
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prefixes = args.prefixes or [DEFAULT_PREFIX]
    for prefix in prefixes:
        if not (1 <= len(prefix) <= 14 and prefix.isdigit()):
            sys.exit(f"非法 GTIN 前缀: {prefix!r}(须为 1~14 位数字)")
    if not (len(args.gtin) == 14 and args.gtin.isdigit() and gtin_check_ok(args.gtin)):
        sys.exit(f"非法 GTIN: {args.gtin!r}(须为通过模 10 校验的 14 位数字)")
    if not any(args.gtin.startswith(prefix) for prefix in prefixes):
        sys.exit(
            f"样例 GTIN {args.gtin} 不以任一授权前缀开头: {prefixes};"
            "请用 --prefix 增加前缀,或用 --gtin 指定其他 GTIN"
        )

    now = args.now if args.now is not None else int(time.time())
    nbf = now + args.nbf_offset
    exp = now + args.exp_offset
    if not nbf <= now < exp:
        sys.exit(f"时间窗口非法: nbf={nbf}, now={now}, exp={exp}")

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    trust_doc = {
        "keys": [
            {
                "kid": args.kid,
                "public_key": b64url(public_raw(public_key)),
                "gtin_prefixes": prefixes,
            }
        ]
    }
    private_doc = {
        "warning": "本地样例私钥,切勿用于生产;服务端不读取本文件",
        "keys": [{"kid": args.kid, "private_key": b64url(private_raw(private_key))}],
    }

    rules = [
        {
            "id": "recall-2026-001",
            "gtin": args.gtin,
            "batch": "LOT1",
            "date_from": "2025-01-01",
            "date_to": "2026-12-31",
        }
    ]
    scans = ["]d2" + "01" + args.gtin + "17" + "251231" + "10" + "LOT1" + FNC1 + "21SN9"]
    payload = {"nbf": nbf, "exp": exp, "rules": rules}
    jws = compact_jws(private_key, args.kid, payload)
    request_doc = {"scans": scans, "jws": jws, "now": now}

    write_file(
        args.trust_file, json.dumps(trust_doc, ensure_ascii=False, indent=2) + "\n", args.force
    )
    write_file(
        args.private_file,
        json.dumps(private_doc, ensure_ascii=False, indent=2) + "\n",
        args.force,
    )
    write_file(
        args.request_file,
        json.dumps(request_doc, ensure_ascii=False, indent=2) + "\n",
        args.force,
    )

    print("已生成样例文件:")
    print(f"  信任文件:   {os.path.relpath(args.trust_file, REPO_ROOT)}")
    print(f"  样例私钥:   {os.path.relpath(args.private_file, REPO_ROOT)}")
    print(f"  请求样例:   {os.path.relpath(args.request_file, REPO_ROOT)}")
    print(f"  kid={args.kid}  授权前缀={prefixes}  nbf={nbf}  now={now}  exp={exp}")
    print()
    print("调用:")
    print(
        "  curl -s http://localhost:8000/v1/recall/check-signed \\\n"
        "    -H 'Content-Type: application/json' \\\n"
        f"    --data @{os.path.relpath(args.request_file, REPO_ROOT)}"
    )
    print()
    print("注意:时间窗口随生成时刻而定,过期后重新运行本脚本即可。")


if __name__ == "__main__":
    main()
