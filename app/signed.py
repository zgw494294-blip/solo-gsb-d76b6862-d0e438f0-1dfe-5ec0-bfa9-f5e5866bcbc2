"""POST /v1/recall/check-signed:验签后执行扫码核验。

请求体含现有 scans、compact JWS 字符串 jws 与整数 now(Unix 秒)。
JWS 三段均为无填充 base64url:

- 头部仅含 "alg": "EdDSA" 与非空 "kid";
- 载荷仅含整数 "nbf"、"exp" 及现有格式 "rules"(Rule 列表,1~100 条、ID 唯一);
- 签名为头部.载荷(原始前两段以点连接后的字节)的 Ed25519 签名。

头部 / 载荷 JSON 任一层出现重复键即拒绝。公钥按 kid 从本地信任文件查取
(见 trust.py),不联网取钥。

状态码约定:

- 422:JWS 结构 / 编码 / JSON / 头部字段 / 载荷字段或规则格式非法,或请求体非法;
- 403:未知 kid、签名验证失败、nbf/exp 越期(须 nbf <= now < exp)、
  任一规则 GTIN 不落在该 kid 的授权前缀内、规则包在本地单包撤销名单中命中;
- 500:本地信任文件缺失或内容非法、已配置的撤销名单不可读或内容非法
  (均为服务端配置问题)。

任何 422 / 403 都不返回匹配结果。

单包撤销(见 revoked.py):配置环境变量 RECALL_REVOKED_FILE 指向仅含
"revoked" 数组(64 位小写十六进制 SHA-256 摘要,允许重复)的本地 JSON 后,
在验签、时间窗与 GTIN 授权全部通过后,按包标识
SHA-256(header.payload 原始两段 ASCII 字节,与签名段编码无关)命中名单即
返回 403;同一 kid 的其他包不受影响。未配置 RECALL_REVOKED_FILE 时保持旧行为。
名单每次请求读盘一次,宿主机原子替换后下一次请求即生效。
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from fastapi import HTTPException
from pydantic import ValidationError

from .checker import check_scans
from .jws import JWSFormatError, b64url_decode, json_decode_b64url
from .models import Rule, ScanResult, SignedCheckRequest, SignedCheckResponse
from .revoked import (
    DEFAULT_REVOKED_FILE,
    RevocationListError,
    load_revoked_digests,
    package_digest,
)
from .trust import (
    DEFAULT_TRUST_FILE,
    TrustStoreError,
    TrustedKey,
    UnknownKeyError,
    load_trusted_key,
)


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(status_code=422, detail=reason)


def _forbidden(reason: str) -> HTTPException:
    return HTTPException(status_code=403, detail=reason)


def _validation_reason(exc: ValidationError) -> str:
    errors = exc.errors()
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    if location:
        return f"载荷 rules 非法: {location}: {first.get('msg')}"
    return f"载荷 rules 非法: {first.get('msg')}"


def _split_segments(jws: str) -> tuple[str, str, str]:
    segments = jws.split(".")
    if len(segments) != 3:
        raise _unprocessable("JWS 须为由两个点分隔的三段 compact 序列化字符串")
    header_seg, payload_seg, signature_seg = segments
    if not header_seg or not payload_seg or not signature_seg:
        raise _unprocessable("JWS 的头部 / 载荷 / 签名段均不得为空")
    return header_seg, payload_seg, signature_seg


def _parse_header(header_seg: str) -> str:
    try:
        header = json_decode_b64url(header_seg)
    except JWSFormatError as exc:
        raise _unprocessable(f"JWS 头部: {exc}") from exc
    if not isinstance(header, dict):
        raise _unprocessable("JWS 头部须为 JSON 对象")
    if set(header.keys()) != {"alg", "kid"}:
        raise _unprocessable("JWS 头部仅允许 'alg' 与 'kid' 两个键")
    if header["alg"] != "EdDSA":
        raise _unprocessable("JWS 头部 alg 须为 'EdDSA'")
    kid = header["kid"]
    if not isinstance(kid, str) or not kid:
        raise _unprocessable("JWS 头部 kid 须为非空字符串")
    return kid


def _parse_payload(
    payload_seg: str,
) -> tuple[int, int, list[Rule]]:
    try:
        payload = json_decode_b64url(payload_seg)
    except JWSFormatError as exc:
        raise _unprocessable(f"JWS 载荷: {exc}") from exc
    if not isinstance(payload, dict):
        raise _unprocessable("JWS 载荷须为 JSON 对象")
    if set(payload.keys()) != {"nbf", "exp", "rules"}:
        raise _unprocessable("JWS 载荷仅允许 'nbf'、'exp'、'rules' 三个键")
    nbf = payload["nbf"]
    exp = payload["exp"]
    raw_rules = payload["rules"]
    # bool 是 int 的子类,须显式排除
    if not isinstance(nbf, int) or isinstance(nbf, bool):
        raise _unprocessable("JWS 载荷 nbf 须为整数(Unix 秒)")
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise _unprocessable("JWS 载荷 exp 须为整数(Unix 秒)")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _unprocessable("JWS 载荷 rules 须为非空数组(1~100 条规则)")

    try:
        # 复用 Rule 的全部校验(GTIN 模 10、字段长度、日期区间)与 ID 唯一性
        rules = Rule.model_validate_list(raw_rules)
    except ValidationError as exc:
        raise _unprocessable(_validation_reason(exc)) from exc
    except ValueError as exc:
        raise _unprocessable(f"载荷 rules 非法: {exc}") from exc
    if len(rules) > 100:
        raise _unprocessable("载荷 rules 非法: 规则数量须为 1~100 条")
    return nbf, exp, rules


def _verify_signature(
    header_seg: str, payload_seg: str, signature_seg: str, trusted: TrustedKey
) -> None:
    try:
        signature = b64url_decode(signature_seg)
    except JWSFormatError as exc:
        raise _unprocessable(f"JWS 签名段: {exc}") from exc
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    try:
        trusted.public_key.verify(signature, signing_input)
    except InvalidSignature as exc:
        raise _forbidden("签名验证失败") from exc
    except ValueError as exc:  # 签名长度非法等
        raise _forbidden("签名验证失败") from exc


def _check_time_window(nbf: int, exp: int, now: int) -> None:
    if not nbf <= now < exp:
        raise _forbidden(f"令牌时间窗口越期: 须 nbf({nbf}) <= now({now}) < exp({exp})")


def _check_prefix_authorization(rules: list[Rule], trusted: TrustedKey) -> None:
    for rule in rules:
        if not any(rule.gtin.startswith(prefix) for prefix in trusted.gtin_prefixes):
            raise _forbidden(
                f"规则 {rule.id!r} 的 GTIN {rule.gtin} 不属于密钥 "
                f"{trusted.kid!r} 授权的 GTIN 前缀 {list(trusted.gtin_prefixes)}"
            )


def _check_not_revoked(header_seg: str, payload_seg: str) -> None:
    """单包撤销:未配置名单时放行;名单不可读 / 非法按 500 失败关闭。

    包标识只取 JWS 原始前两段,签名段编码变化不影响标识,因此撤销仅命中
    该包本身,不连带同一 kid 的其他包。
    """
    if not DEFAULT_REVOKED_FILE:
        return
    try:
        revoked = load_revoked_digests(DEFAULT_REVOKED_FILE)
    except RevocationListError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    digest = package_digest(header_seg, payload_seg)
    if digest in revoked:
        raise _forbidden(f"规则包已被撤销: {digest}")


def check_signed(request: SignedCheckRequest) -> SignedCheckResponse:
    """验签 + 时间窗 + 授权前缀全部通过后,返回 kid 与原扫码核验结果。"""
    header_seg, payload_seg, signature_seg = _split_segments(request.jws)
    kid = _parse_header(header_seg)
    nbf, exp, rules = _parse_payload(payload_seg)

    # 未知 kid 属于授权失败(403),不暴露匹配结果
    try:
        trusted = load_trusted_key(kid, DEFAULT_TRUST_FILE)
    except UnknownKeyError as exc:
        raise _forbidden(str(exc)) from exc
    except TrustStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _verify_signature(header_seg, payload_seg, signature_seg, trusted)
    _check_time_window(nbf, exp, request.now)
    _check_prefix_authorization(rules, trusted)
    _check_not_revoked(header_seg, payload_seg)

    results: list[ScanResult] = check_scans(request.scans, rules)
    return SignedCheckResponse(kid=kid, results=results)
