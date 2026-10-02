"""POST /v1/recall/check-signed 的端到端测试。"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import signed
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

FNC1 = "\x1d"  # ASCII 29

KID = "recall-key-test-001"
OTHER_KID = "recall-key-test-002"
GTIN = "09506001123454"  # make_gtin("0950600112345")
GTIN2 = "09506001123461"  # 同前缀 0950600
GTIN_OTHER = "08591234567893"  # make_gtin("0859123456789"),不同前缀
NBF = 1_789_999_000
NOW = 1_790_000_000
EXP = 1_790_086_400

SCAN = "]d2" + "01" + GTIN + "17" + "251231" + "10" + "LOT1" + FNC1 + "21SN9"
RULE = {
    "id": "recall-2026-001",
    "gtin": GTIN,
    "batch": "LOT1",
    "date_from": "2025-01-01",
    "date_to": "2026-12-31",
}


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def other_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture(autouse=True)
def trust_file(tmp_path, monkeypatch, private_key, other_private_key):
    """把模块默认信任文件路径指向临时文件,内含测试 kid。"""
    path = tmp_path / "trust.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "kid": KID,
                        "public_key": _public_b64(private_key),
                        "gtin_prefixes": ["0950600"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(signed, "DEFAULT_TRUST_FILE", str(path))
    return path


@pytest.fixture
def revocation_file(tmp_path, monkeypatch):
    """启用单包撤销:把默认名单路径指向临时空名单,返回可改写的路径。"""
    path = tmp_path / "revoked.json"
    path.write_text(json.dumps({"revoked": []}), encoding="utf-8")
    monkeypatch.setattr(signed, "DEFAULT_REVOKED_FILE", str(path))
    return path


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url(raw)


def make_jws(
    private_key: Ed25519PrivateKey,
    payload: object,
    *,
    kid: str = KID,
    header: dict | None = None,
    sign: bool = True,
    bad_signature: bool = False,
    raw_header: str | None = None,
    raw_payload: str | None = None,
) -> str:
    header_obj = {"alg": "EdDSA", "kid": kid} if header is None else header
    header_seg = (
        raw_header
        if raw_header is not None
        else _b64url(json.dumps(header_obj, separators=(",", ":")).encode())
    )
    payload_seg = (
        raw_payload
        if raw_payload is not None
        else _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    if not sign:
        signature_seg = ""
    else:
        signature = private_key.sign(f"{header_seg}.{payload_seg}".encode("ascii"))
        if bad_signature:
            signature = bytes([signature[0] ^ 0xFF]) + signature[1:]
        signature_seg = _b64url(signature)
    return f"{header_seg}.{payload_seg}.{signature_seg}"


def valid_payload(**overrides) -> dict:
    payload = {"nbf": NBF, "exp": EXP, "rules": [RULE]}
    payload.update(overrides)
    return payload


def post(jws: str, *, scans=None, now: int = NOW):
    return client.post(
        "/v1/recall/check-signed",
        json={"scans": scans if scans is not None else [SCAN], "jws": jws, "now": now},
    )


def _package_digest(jws: str) -> str:
    import hashlib

    header_seg, payload_seg, _signature_seg = jws.split(".")
    return hashlib.sha256(f"{header_seg}.{payload_seg}".encode("ascii")).hexdigest()


# ---------- 正常路径 ----------


def test_valid_signed_check(private_key):
    resp = post(make_jws(private_key, valid_payload()))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kid"] == KID
    (result,) = body["results"]
    assert result["ok"] is True
    assert result["fields"] == {
        "gtin": GTIN,
        "batch": "LOT1",
        "serial": "SN9",
        "date": "2025-12-31",
    }
    assert result["matches"] == ["recall-2026-001"]
    assert result["error"] is None


def test_per_scan_parse_error_still_inline(private_key):
    bad_scan = "01" + GTIN + "99"
    resp = post(make_jws(private_key, valid_payload()), scans=[SCAN, bad_scan, SCAN])
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["ok"] for r in results] == [True, False, True]
    assert results[1]["error"]["reason"].startswith("未知 AI")
    assert results[0]["matches"] == ["recall-2026-001"]


def test_nbf_inclusive_lower_bound(private_key):
    payload = valid_payload(nbf=NOW)
    assert post(make_jws(private_key, payload), now=NOW).status_code == 200


def test_exp_exclusive_upper_bound(private_key):
    payload = valid_payload(exp=NOW)
    resp = post(make_jws(private_key, payload), now=NOW)
    assert resp.status_code == 403
    assert "越期" in resp.json()["detail"]


def test_time_window_before_nbf_is_403(private_key):
    resp = post(make_jws(private_key, valid_payload(nbf=NOW + 1)), now=NOW)
    assert resp.status_code == 403


def test_time_window_after_exp_is_403(private_key):
    resp = post(make_jws(private_key, valid_payload(exp=NOW)), now=NOW + 1)
    assert resp.status_code == 403


# ---------- 授权前缀 ----------


def test_unauthorized_gtin_prefix_is_403(private_key):
    rule = {**RULE, "id": "foreign", "gtin": GTIN_OTHER}
    resp = post(make_jws(private_key, valid_payload(rules=[rule])))
    assert resp.status_code == 403
    assert "授权" in resp.json()["detail"]


def test_every_rule_must_be_authorized(private_key):
    rule2 = {**RULE, "id": "recall-2026-002", "gtin": GTIN_OTHER}
    resp = post(make_jws(private_key, valid_payload(rules=[RULE, rule2])))
    assert resp.status_code == 403


def test_shorter_prefix_matches(private_key, trust_file):
    # 14 位前缀即精确相等:换成精确前缀后,其他 GTIN 不再被授权
    trust_file.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "kid": KID,
                        "public_key": _public_b64(private_key),
                        "gtin_prefixes": [GTIN],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert post(make_jws(private_key, valid_payload())).status_code == 200
    rule2 = {**RULE, "id": "r2", "gtin": GTIN2}
    assert post(make_jws(private_key, valid_payload(rules=[rule2]))).status_code == 403


# ---------- 签名 / 密钥 ----------


def test_bad_signature_is_403(private_key):
    resp = post(make_jws(private_key, valid_payload(), bad_signature=True))
    assert resp.status_code == 403
    assert "签名验证失败" in resp.json()["detail"]


def test_wrong_key_is_403(private_key, other_private_key):
    # 用信任文件中不存在的密钥签名,但头部 kid 仍指向受信 kid
    resp = post(make_jws(other_private_key, valid_payload()))
    assert resp.status_code == 403


def test_unknown_kid_is_403(private_key):
    resp = post(make_jws(private_key, valid_payload(), kid="nobody"))
    assert resp.status_code == 403
    assert "未知密钥" in resp.json()["detail"]


def test_missing_trust_file_is_500(private_key, monkeypatch):
    monkeypatch.setattr(signed, "DEFAULT_TRUST_FILE", "/nonexistent/trust.json")
    resp = post(make_jws(private_key, valid_payload()))
    assert resp.status_code == 500


# ---------- JWS 结构 / 编码:422 ----------


def test_wrong_segment_count_is_422(private_key):
    jws = make_jws(private_key, valid_payload())
    for broken in (jws + ".x", "a.b", "abc", ""):
        resp = post(broken)
        assert resp.status_code in (422,), broken
        assert resp.status_code == 422, broken


def test_empty_segment_is_422(private_key):
    jws = make_jws(private_key, valid_payload(), sign=False)
    assert post(jws).status_code == 422  # 空签名段
    # 空头 / 空载荷段
    assert post(".x.y").status_code == 422


def test_base64_padding_rejected(private_key):
    header_seg = _b64url(json.dumps({"alg": "EdDSA", "kid": KID}).encode()) + "="
    payload_seg = _b64url(json.dumps(valid_payload()).encode())
    sig = _b64url(private_key.sign(f"{header_seg}.{payload_seg}".encode()))
    assert post(f"{header_seg}.{payload_seg}.{sig}").status_code == 422


def test_base64_illegal_character_rejected(private_key):
    jws = make_jws(private_key, valid_payload())
    header_seg, payload_seg, sig = jws.split(".")
    assert post(f"{header_seg}!.{payload_seg}.{sig}").status_code == 422
    # 标准 base64 的 '+' / '/' 也不允许
    assert post(f"{header_seg}+.{payload_seg}.{sig}").status_code == 422


# ---------- 头部:422 ----------


def test_header_alg_wrong(private_key):
    jws = make_jws(private_key, valid_payload(), header={"alg": "ES256", "kid": KID})
    assert post(jws).status_code == 422
    jws = make_jws(private_key, valid_payload(), header={"alg": "EdDSA", "kid": ""})
    assert post(jws).status_code == 422
    jws = make_jws(private_key, valid_payload(), header={"alg": "EdDSA", "kid": 1})
    assert post(jws).status_code == 422


def test_header_extra_or_missing_keys(private_key):
    jws = make_jws(
        private_key, valid_payload(), header={"alg": "EdDSA", "kid": KID, "typ": "JWT"}
    )
    assert post(jws).status_code == 422
    jws = make_jws(private_key, valid_payload(), header={"alg": "EdDSA"})
    assert post(jws).status_code == 422
    jws = make_jws(private_key, valid_payload(), header={"kid": KID})
    assert post(jws).status_code == 422


def test_header_not_object(private_key):
    raw = _b64url(b"[1,2]")
    payload_seg = _b64url(json.dumps(valid_payload()).encode())
    jws = f"{raw}.{payload_seg}.x"
    assert post(jws).status_code == 422


def test_header_duplicate_json_keys(private_key):
    # {"alg":"EdDSA","kid":"...","kid":"..."} 手工构造重复键
    dup = (
        '{"alg":"EdDSA","kid":"' + KID + '","kid":"' + KID + '"}'
    ).encode()
    payload_seg = _b64url(json.dumps(valid_payload()).encode())
    header_seg = _b64url(dup)
    sig = _b64url(private_key.sign(f"{header_seg}.{payload_seg}".encode()))
    assert post(f"{header_seg}.{payload_seg}.{sig}").status_code == 422


# ---------- 载荷结构:422 ----------


def test_payload_not_object(private_key):
    jws = make_jws(private_key, [1, 2, 3])
    assert post(jws).status_code == 422


def test_payload_extra_or_missing_keys(private_key):
    jws = make_jws(private_key, {"nbf": NBF, "exp": EXP, "rules": [RULE], "iss": "x"})
    assert post(jws).status_code == 422
    jws = make_jws(private_key, {"exp": EXP, "rules": [RULE]})
    assert post(jws).status_code == 422
    jws = make_jws(private_key, {"nbf": NBF, "exp": EXP})
    assert post(jws).status_code == 422


def test_payload_nbf_exp_must_be_integer(private_key):
    jws = make_jws(private_key, valid_payload(nbf="1789999000"))
    assert post(jws).status_code == 422
    jws = make_jws(private_key, valid_payload(nbf=1.5))
    assert post(jws).status_code == 422
    jws = make_jws(private_key, valid_payload(exp=True))
    assert post(jws).status_code == 422


def test_payload_rules_must_be_nonempty_list(private_key):
    jws = make_jws(private_key, valid_payload(rules=[]))
    assert post(jws).status_code == 422
    jws = make_jws(private_key, valid_payload(rules=RULE))
    assert post(jws).status_code == 422


def test_payload_duplicate_top_level_keys(private_key):
    # {"nbf":..,"exp":..,"rules":[..],"rules":[..]}
    dup = (
        '{"nbf":%d,"exp":%d,"rules":[%s],"rules":[%s]}'
        % (NBF, EXP, json.dumps(RULE, separators=(",", ":")), json.dumps(RULE, separators=(",", ":")))
    ).encode()
    header_seg = _b64url(json.dumps({"alg": "EdDSA", "kid": KID}).encode())
    payload_seg = _b64url(dup)
    sig = _b64url(private_key.sign(f"{header_seg}.{payload_seg}".encode()))
    assert post(f"{header_seg}.{payload_seg}.{sig}").status_code == 422


def test_payload_duplicate_key_inside_rule(private_key):
    dup_rule = (
        '{"id":"r","id":"r","gtin":"%s"}' % GTIN
    ).encode()
    dup_payload = (
        b'{"nbf":%d,"exp":%d,"rules":[%s]}' % (NBF, EXP, dup_rule)
    )
    header_seg = _b64url(json.dumps({"alg": "EdDSA", "kid": KID}).encode())
    payload_seg = _b64url(dup_payload)
    sig = _b64url(private_key.sign(f"{header_seg}.{payload_seg}".encode()))
    assert post(f"{header_seg}.{payload_seg}.{sig}").status_code == 422


# ---------- 载荷规则格式:422(即使越权也先按格式) ----------


def test_invalid_rule_gtin_check_digit_is_422(private_key):
    bad_gtin = GTIN[:-1] + str((int(GTIN[-1]) + 1) % 10)
    jws = make_jws(private_key, valid_payload(rules=[{**RULE, "gtin": bad_gtin}]))
    assert post(jws).status_code == 422


def test_duplicate_rule_ids_is_422(private_key):
    jws = make_jws(
        private_key,
        valid_payload(rules=[RULE, {**RULE, "gtin": GTIN2}]),
    )
    assert post(jws).status_code == 422


def test_rule_date_range_reversed_is_422(private_key):
    jws = make_jws(
        private_key,
        valid_payload(
            rules=[{**RULE, "date_from": "2026-01-01", "date_to": "2025-01-01"}]
        ),
    )
    assert post(jws).status_code == 422


def test_rule_extra_field_is_422(private_key):
    jws = make_jws(
        private_key, valid_payload(rules=[{**RULE, "unexpected": 1}])
    )
    assert post(jws).status_code == 422


def test_too_many_rules_is_422(private_key):
    rules = [{**RULE, "id": f"r{i}"} for i in range(101)]
    jws = make_jws(private_key, valid_payload(rules=rules))
    assert post(jws).status_code == 422


# ---------- 请求体:422 ----------


def test_request_body_validation(private_key):
    jws = make_jws(private_key, valid_payload())

    def raw_post(body):
        return client.post("/v1/recall/check-signed", json=body)

    assert raw_post({"scans": [], "jws": jws, "now": NOW}).status_code == 422
    assert raw_post({"scans": [SCAN] * 101, "jws": jws, "now": NOW}).status_code == 422
    assert raw_post({"scans": [SCAN], "jws": "", "now": NOW}).status_code == 422
    assert raw_post({"scans": [SCAN], "jws": jws}).status_code == 422  # 缺 now
    assert raw_post({"scans": [SCAN], "jws": jws, "now": True}).status_code == 422
    assert raw_post({"scans": [SCAN], "jws": jws, "now": NOW, "x": 1}).status_code == 422


# ---------- 签名输入必须是原始前两段 ----------


def test_signature_over_decoded_bytes_fails(private_key):
    header_seg = _b64url(json.dumps({"alg": "EdDSA", "kid": KID}).encode())
    payload_seg = _b64url(json.dumps(valid_payload()).encode())
    # 错误做法:对解码后的字节签名
    import base64

    decoded = (
        base64.urlsafe_b64decode(header_seg + "==")
        + b"."
        + base64.urlsafe_b64decode(payload_seg + "==")
    )
    sig = _b64url(private_key.sign(decoded))
    resp = post(f"{header_seg}.{payload_seg}.{sig}")
    assert resp.status_code == 403


# ---------- 单包撤销 ----------


def test_revocation_unconfigured_keeps_legacy_behavior(private_key, monkeypatch):
    # 未配置名单(空路径):与旧行为一致,正常返回 200
    monkeypatch.setattr(signed, "DEFAULT_REVOKED_FILE", "")
    resp = post(make_jws(private_key, valid_payload()))
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["matches"] == ["recall-2026-001"]


def test_empty_revocation_list_allows_package(private_key, revocation_file):
    assert post(make_jws(private_key, valid_payload())).status_code == 200


def test_revoked_package_is_403_without_results(private_key, revocation_file):
    jws = make_jws(private_key, valid_payload())
    digest = _package_digest(jws)
    revocation_file.write_text(json.dumps({"revoked": [digest]}), encoding="utf-8")
    resp = post(jws)
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert digest in body["detail"]
    assert "撤销" in body["detail"]
    assert "results" not in body and "kid" not in body


def test_revocation_is_single_package_per_kid(private_key, revocation_file):
    # 同一 kid 签发两个不同载荷的包:撤销其一不连带另一个
    jws1 = make_jws(private_key, valid_payload())
    rule2 = {**RULE, "id": "recall-2026-002", "gtin": GTIN2}
    scan2 = "]d2" + "01" + GTIN2 + "17" + "251231" + "10" + "LOT1" + FNC1 + "21SN9"
    jws2 = make_jws(private_key, valid_payload(rules=[rule2]))
    revocation_file.write_text(
        json.dumps({"revoked": [_package_digest(jws1)]}), encoding="utf-8"
    )
    assert post(jws1).status_code == 403
    resp2 = post(jws2, scans=[scan2])
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["results"][0]["matches"] == ["recall-2026-002"]


def test_signature_segment_encoding_does_not_change_identity(
    private_key, revocation_file
):
    # 包标识只取原始前两段:重签名 / 签名段不同,头与载荷字节一致仍命中同一摘要
    jws = make_jws(private_key, valid_payload())
    header_seg, payload_seg, _sig = jws.split(".")
    other_sig = _b64url(private_key.sign(f"{header_seg}.{payload_seg}".encode("ascii")))
    revocation_file.write_text(
        json.dumps({"revoked": [_package_digest(jws)]}), encoding="utf-8"
    )
    assert post(f"{header_seg}.{payload_seg}.{other_sig}").status_code == 403


def test_atomic_replace_takes_effect_on_next_request(
    private_key, revocation_file, tmp_path
):
    import os

    jws = make_jws(private_key, valid_payload())
    digest = _package_digest(jws)

    def atomic_write(revoked: list[str]) -> None:
        tmp = revocation_file.with_name("revoked.tmp")
        tmp.write_text(json.dumps({"revoked": revoked}), encoding="utf-8")
        os.replace(tmp, revocation_file)  # rename(2) 原子替换

    assert post(jws).status_code == 200
    atomic_write([digest])
    assert post(jws).status_code == 403  # 不重启,下一次请求即生效
    atomic_write([])
    assert post(jws).status_code == 200


def test_duplicate_digest_entries_allowed(private_key, revocation_file):
    jws = make_jws(private_key, valid_payload())
    digest = _package_digest(jws)
    revocation_file.write_text(
        json.dumps({"revoked": [digest, digest, digest]}), encoding="utf-8"
    )
    assert post(jws).status_code == 403


def test_revocation_check_runs_after_other_gates(private_key, revocation_file):
    # 撤销名单已配置且(稍后)命中,但先于撤销的失败仍按各自状态码返回
    jws = make_jws(private_key, valid_payload())
    digest = _package_digest(jws)
    revocation_file.write_text(json.dumps({"revoked": [digest]}), encoding="utf-8")

    assert post(make_jws(private_key, valid_payload(), kid="nobody")).status_code == 403
    assert post(make_jws(private_key, valid_payload(), bad_signature=True)).status_code == 403
    assert post(jws, now=EXP).status_code == 403  # 时间窗越期
    rule_other = {**RULE, "gtin": GTIN_OTHER}
    assert post(make_jws(private_key, valid_payload(rules=[rule_other]))).status_code == 403


def test_revocation_list_opened_exactly_once_per_passing_request(
    private_key, revocation_file, monkeypatch
):
    # 前置检查全部通过的请求,名单只读盘一次(不重复打开 / 无缓存回退)
    import builtins

    real_open = builtins.open
    target = str(revocation_file)
    opens: list[str] = []

    def counting_open(file, *args, **kwargs):
        if str(file) == target:
            opens.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    resp = post(make_jws(private_key, valid_payload()))
    assert resp.status_code == 200, resp.text
    assert opens == [target]


def test_revocation_not_opened_when_earlier_gate_fails(private_key, monkeypatch):
    # 未通过前置检查的请求不读名单:未配置 + 未知 kid 时不触碰任何名单文件
    import builtins

    monkeypatch.setattr(signed, "DEFAULT_REVOKED_FILE", "")
    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    assert post(make_jws(private_key, valid_payload(), kid="nobody")).status_code == 403
    assert not any("revoked" in name for name in opened)


def test_missing_revocation_file_is_500(private_key, revocation_file, monkeypatch):
    monkeypatch.setattr(signed, "DEFAULT_REVOKED_FILE", "/nonexistent/revoked.json")
    resp = post(make_jws(private_key, valid_payload()))
    assert resp.status_code == 500
    assert "results" not in resp.json()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        "{}",
        '{"revoked": [],"extra": 1}',
        '{"revoked": "x"}',
        '{"revoked": [1]}',
        '{"revoked": ["ABC"]}',
        # 64 位但含大写
        '{"revoked": ["9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08"]}',
        # 长度 63
        '{"revoked": ["9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a0"]}',
    ],
)
def test_invalid_revocation_list_is_500(private_key, revocation_file, content):
    revocation_file.write_text(content, encoding="utf-8")
    resp = post(make_jws(private_key, valid_payload()))
    assert resp.status_code == 500, resp.text
    assert "results" not in resp.json()


def test_invalid_revocation_list_does_not_fallback_to_old_content(
    private_key, revocation_file
):
    # 先以合法(空)名单放行,再把文件改成非法内容:不得沿用旧名单放行
    jws = make_jws(private_key, valid_payload())
    assert post(jws).status_code == 200
    revocation_file.write_text("BROKEN", encoding="utf-8")
    resp = post(jws)
    assert resp.status_code == 500
    assert "results" not in resp.json()
    # 恢复为命中该包的合法名单后立即生效为 403
    revocation_file.write_text(
        json.dumps({"revoked": [_package_digest(jws)]}), encoding="utf-8"
    )
    assert post(jws).status_code == 403
