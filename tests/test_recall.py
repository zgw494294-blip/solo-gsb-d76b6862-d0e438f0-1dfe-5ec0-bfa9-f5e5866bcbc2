"""POST /v1/recall/check 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FNC1 = "\x1d"  # ASCII 29


def make_gtin(body13: str) -> str:
    """由 13 位本体计算模 10 校验位,生成合法 14 位 GTIN。"""
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")


def post(scans, rules):
    resp = client.post("/v1/recall/check", json={"scans": scans, "rules": rules})
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


def rule(rid, gtin=GTIN, **kw):
    return {"id": rid, "gtin": gtin, **kw}


# ---------- 解析:正常路径 ----------

def test_parse_all_ais_with_prefix_and_fnc1():
    scan = "]d2" + "01" + GTIN + "17" + "251231" + "10" + "LOT1" + FNC1 + "21" + "SN9"
    (res,) = post([scan], [rule("R1")])
    assert res["ok"] is True
    assert res["fields"] == {
        "gtin": GTIN,
        "batch": "LOT1",
        "serial": "SN9",
        "date": "2025-12-31",
    }
    assert res["matches"] == ["R1"]
    assert res["error"] is None


def test_parse_ais_in_any_order():
    scan = "21" + "SN9" + FNC1 + "17" + "251231" + "10" + "LOT1" + FNC1 + "01" + GTIN
    (res,) = post([scan], [rule("R1")])
    assert res["ok"] is True
    assert res["fields"]["gtin"] == GTIN
    assert res["fields"]["batch"] == "LOT1"
    assert res["fields"]["serial"] == "SN9"


def test_variable_field_ends_only_at_fnc1_or_end():
    # 无 FNC1 时,"21" 只是批号内容的一部分
    (res,) = post(["01" + GTIN + "10" + "ABC21DEF"], [rule("R1")])
    assert res["ok"] is True
    assert res["fields"]["batch"] == "ABC21DEF"
    assert res["fields"]["serial"] is None
    # FNC1 后才开启新 AI
    (res,) = post(["01" + GTIN + "10" + "ABC" + FNC1 + "21" + "SN"], [rule("R1")])
    assert res["fields"]["batch"] == "ABC"
    assert res["fields"]["serial"] == "SN"


def test_gtin_only_scan():
    (res,) = post(["01" + GTIN], [rule("R1")])
    assert res["ok"] is True
    assert res["fields"] == {"gtin": GTIN, "batch": None, "serial": None, "date": None}


def test_date_day00_means_last_day_of_month():
    (res,) = post(["01" + GTIN + "17" + "250200"], [rule("R1")])
    assert res["fields"]["date"] == "2025-02-28"
    # 闰年
    (res,) = post(["01" + GTIN + "17" + "240200"], [rule("R1")])
    assert res["fields"]["date"] == "2024-02-29"


def test_year_interpreted_as_2000_to_2099():
    (res,) = post(["01" + GTIN + "17" + "990101"], [rule("R1")])
    assert res["fields"]["date"] == "2099-01-01"
    (res,) = post(["01" + GTIN + "17" + "000101"], [rule("R1")])
    assert res["fields"]["date"] == "2000-01-01"


def test_variable_field_max_length_20():
    (res,) = post(["01" + GTIN + "10" + "A" * 20], [rule("R1")])
    assert res["ok"] is True
    assert res["fields"]["batch"] == "A" * 20


# ---------- 解析:错误路径(偏移 + 原因) ----------

def test_unknown_ai_offset_with_prefix():
    (res,) = post(["]d2" + "01" + GTIN + "99" + "AB"], [rule("R1")])
    assert res["ok"] is False
    assert res["error"]["offset"] == 3 + 2 + 14
    assert "未知 AI" in res["error"]["reason"]
    assert res["fields"] is None
    assert res["matches"] == []


def test_duplicate_ai():
    (res,) = post(["01" + GTIN + "01" + GTIN], [rule("R1")])
    assert res["ok"] is False
    assert "重复" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 14


def test_missing_required_ai_01():
    (res,) = post(["10" + "ABC"], [rule("R1")])
    assert res["ok"] is False
    assert "缺少必填 AI '01'" in res["error"]["reason"]


def test_gtin_check_digit_error_offset():
    bad = GTIN[:-1] + str((int(GTIN[-1]) + 1) % 10)
    (res,) = post(["01" + bad], [rule("R1")])
    assert res["ok"] is False
    assert "校验位" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 13  # 校验位所在位置


def test_illegal_character_in_batch():
    scan = "01" + GTIN + "10" + "AB\x07C"
    (res,) = post([scan], [rule("R1")])
    assert res["ok"] is False
    assert "非法字符" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 14 + 2 + 2  # \x07 的位置


def test_batch_too_long():
    (res,) = post(["01" + GTIN + "10" + "A" * 21], [rule("R1")])
    assert res["ok"] is False
    assert "超过最大长度" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 14 + 2 + 20  # 第 21 个字符


def test_batch_empty():
    (res,) = post(["01" + GTIN + "10" + FNC1 + "21" + "SN"], [rule("R1")])
    assert res["ok"] is False
    assert "值为空" in res["error"]["reason"]


def test_fixed_field_truncated():
    (res,) = post(["01" + GTIN + "17" + "2512"], [rule("R1")])
    assert res["ok"] is False
    assert "长度不足" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 14 + 2 + 4


def test_fixed_field_short_by_separator():
    (res,) = post(["01" + GTIN[:13] + FNC1 + "10" + "ABC"], [rule("R1")])
    assert res["ok"] is False
    assert "长度不足" in res["error"]["reason"]


def test_unexpected_separator():
    # 双 FNC1
    (res,) = post(["01" + GTIN + FNC1 + FNC1 + "10" + "ABC"], [rule("R1")])
    assert res["ok"] is False
    assert "分隔符" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 14 + 1
    # 起始 FNC1
    (res,) = post([FNC1 + "01" + GTIN], [rule("R1")])
    assert res["ok"] is False
    assert res["error"]["offset"] == 0


def test_invalid_month_and_day():
    (res,) = post(["01" + GTIN + "17" + "251301"], [rule("R1")])
    assert res["ok"] is False
    assert "月份非法" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 14 + 2 + 2  # MM 的位置
    (res,) = post(["01" + GTIN + "17" + "250431"], [rule("R1")])  # 4 月无 31 日
    assert res["ok"] is False
    assert "日期非法" in res["error"]["reason"]
    assert res["error"]["offset"] == 2 + 14 + 2 + 4  # DD 的位置


def test_empty_scan():
    (res,) = post([""], [rule("R1")])
    assert res["ok"] is False
    assert res["error"]["offset"] == 0
    (res,) = post(["]d2"], [rule("R1")])
    assert res["ok"] is False
    assert res["error"]["offset"] == 3


def test_bad_scan_does_not_affect_others():
    good = "01" + GTIN
    bad = "01" + GTIN + "99"
    results = post([good, bad, good], [rule("R1")])
    assert [r["ok"] for r in results] == [True, False, True]
    assert results[0]["matches"] == ["R1"]
    assert results[2]["matches"] == ["R1"]
    assert [r["index"] for r in results] == [0, 1, 2]


# ---------- 规则匹配 ----------

def test_match_requires_attached_fields_present():
    # 规则附批号,扫描串无 AI 10 → 不命中
    (res,) = post(["01" + GTIN], [rule("R1", batch="LOT1")])
    assert res["matches"] == []
    # 存在且相等 → 命中
    (res,) = post(["01" + GTIN + "10" + "LOT1"], [rule("R1", batch="LOT1")])
    assert res["matches"] == ["R1"]
    # 存在但不相等 → 不命中
    (res,) = post(["01" + GTIN + "10" + "LOT2"], [rule("R1", batch="LOT1")])
    assert res["matches"] == []


def test_match_serial_and_date_range():
    rules = [rule("R1", serial="SN9", date_from="2025-01-01", date_to="2025-12-31")]
    scan = "01" + GTIN + "17" + "250630" + "21" + "SN9"
    (res,) = post([scan], rules)
    assert res["matches"] == ["R1"]
    # 序列号不符
    (res,) = post(["01" + GTIN + "17" + "250630" + "21" + "SN8"], rules)
    assert res["matches"] == []
    # 无 AI 17 → 日期区间规则不命中
    (res,) = post(["01" + GTIN + "21" + "SN9"], rules)
    assert res["matches"] == []


def test_match_date_range_is_closed_interval():
    rules = [rule("R1", date_from="2025-06-30", date_to="2025-06-30")]
    (res,) = post(["01" + GTIN + "17" + "250630"], rules)
    assert res["matches"] == ["R1"]
    (res,) = post(["01" + GTIN + "17" + "250629"], rules)
    assert res["matches"] == []
    (res,) = post(["01" + GTIN + "17" + "250701"], rules)
    assert res["matches"] == []


def test_matches_follow_rule_order():
    rules = [
        rule("R-last", batch="LOT1"),
        rule("R-first"),
        rule("R-mid", gtin=GTIN2),  # GTIN 不同,不命中
    ]
    (res,) = post(["01" + GTIN + "10" + "LOT1"], rules)
    assert res["matches"] == ["R-last", "R-first"]


def test_gtin_must_match():
    (res,) = post(["01" + GTIN], [rule("R1", gtin=GTIN2)])
    assert res["matches"] == []


# ---------- 请求体验证(422) ----------

def _expect_422(payload):
    resp = client.post("/v1/recall/check", json=payload)
    assert resp.status_code == 422, resp.text


def test_validation_scan_and_rule_counts():
    _expect_422({"scans": [], "rules": [rule("R1")]})
    _expect_422({"scans": ["01" + GTIN] * 101, "rules": [rule("R1")]})
    _expect_422({"scans": ["01" + GTIN], "rules": []})
    _expect_422({"scans": ["01" + GTIN], "rules": [rule(f"R{i}") for i in range(101)]})


def test_validation_rule_ids_unique():
    _expect_422({"scans": ["01" + GTIN], "rules": [rule("R1"), rule("R1")]})


def test_validation_rule_gtin_must_be_valid():
    _expect_422({"scans": ["01" + GTIN], "rules": [rule("R1", gtin="123")]})
    bad = GTIN[:-1] + str((int(GTIN[-1]) + 1) % 10)
    _expect_422({"scans": ["01" + GTIN], "rules": [rule("R1", gtin=bad)]})


def test_validation_rule_fields():
    _expect_422({"scans": ["01" + GTIN], "rules": [rule("R1", batch="A" * 21)]})
    _expect_422({"scans": ["01" + GTIN], "rules": [rule("R1", batch="")]})
    _expect_422({"scans": ["01" + GTIN], "rules": [rule("R1", date_from="2026-01-01", date_to="2025-01-01")]})
    _expect_422({"scans": ["01" + GTIN], "rules": [rule("R1", unknown_field=1)]})


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
