"""POST /v1/recall/rule-relations 的端到端测试。"""

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


def post(rules):
    resp = client.post("/v1/recall/rule-relations", json={"rules": rules})
    assert resp.status_code == 200, resp.text
    return resp.json()


def rule(rid, gtin=GTIN, **kw):
    return {"id": rid, "gtin": gtin, **kw}


def check_matches(scan, rules):
    """复用召回核验接口,返回 scan 命中 rules 中哪些规则 ID。"""
    resp = client.post(
        "/v1/recall/check", json={"scans": [scan], "rules": rules}
    )
    assert resp.status_code == 200, resp.text
    (res,) = resp.json()["results"]
    assert res["ok"] is True, res
    return res["matches"]


# ---------- 不可能命中的规则 ----------

def test_impossible_rules_listed_in_input_order():
    rules = [
        rule("R-ok"),
        rule("R-past", date_to="1999-12-31"),  # 区间完全在 2000 年之前
        rule("R-ok2", batch="LOT1"),
        rule("R-future", date_from="2100-01-01"),  # 区间完全在 2099 年之后
    ]
    body = post(rules)
    assert body["impossible_rule_ids"] == ["R-past", "R-future"]
    # 不可能命中的规则不参与两两比较
    involved = {p["left_id"] for p in body["pairs"]} | {p["right_id"] for p in body["pairs"]}
    assert involved == {"R-ok", "R-ok2"}


def test_impossible_rules_form_no_pairs_even_together():
    rules = [
        rule("R-past", date_to="1990-06-30"),
        rule("R-future", date_from="2101-03-01"),
    ]
    body = post(rules)
    assert body["impossible_rule_ids"] == ["R-past", "R-future"]
    assert body["pairs"] == []


def test_boundary_dates_are_possible():
    rules = [
        rule("R-all"),
        rule("R-first", date_to="2000-01-01"),  # 仅 2000-01-01 一天
        rule("R-last", date_from="2099-12-31"),  # 仅 2099-12-31 一天
    ]
    body = post(rules)
    assert body["impossible_rule_ids"] == []
    by_ids = {(p["left_id"], p["right_id"]): p for p in body["pairs"]}
    # 两个单日区间互不相交,但各自被无约束规则包含
    assert ("R-first", "R-last") not in by_ids
    assert by_ids[("R-all", "R-first")]["witness"] == "01" + GTIN + "17" + "000101"
    assert by_ids[("R-all", "R-last")]["witness"] == "01" + GTIN + "17" + "991231"


# ---------- 关系类别 ----------

def test_equivalent_rules():
    rules = [
        rule("R1", batch="LOT1", date_from="2025-01-01", date_to="2025-12-31"),
        rule("R2", batch="LOT1", date_from="2025-01-01", date_to="2025-12-31"),
    ]
    body = post(rules)
    (pair,) = body["pairs"]
    assert pair["relation"] == "equivalent"
    assert (pair["left_index"], pair["right_index"]) == (0, 1)
    assert (pair["left_id"], pair["right_id"]) == ("R1", "R2")
    assert check_matches(pair["witness"], rules) == ["R1", "R2"]


def test_clamped_full_range_equals_exact_full_range():
    # 夹取到 2000~2099 后与精确全区间相同 → 等价
    rules = [
        rule("R1", date_from="1990-01-01", date_to="2100-12-31"),
        rule("R2", date_from="2000-01-01", date_to="2099-12-31"),
        rule("R3"),  # 不约束日期:扫描串可不含 AI 17,范围更大
    ]
    body = post(rules)
    by_ids = {(p["left_id"], p["right_id"]): p for p in body["pairs"]}
    assert by_ids[("R1", "R2")]["relation"] == "equivalent"
    # 全区间仍要求扫描串含 AI 17,故是不约束日期规则的真子集
    assert by_ids[("R1", "R3")]["relation"] == "right_contains_left"
    assert by_ids[("R2", "R3")]["relation"] == "right_contains_left"
    assert by_ids[("R1", "R3")]["witness"] == "01" + GTIN + "17" + "000101"


def test_left_contains_right_and_reverse():
    rules = [rule("R-all"), rule("R-lot", batch="LOT1")]
    (pair,) = post(rules)["pairs"]
    assert pair["relation"] == "left_contains_right"
    assert pair["witness"] == "01" + GTIN + "10" + "LOT1"

    rules = [rule("R-lot", batch="LOT1"), rule("R-all")]
    (pair,) = post(rules)["pairs"]
    assert pair["relation"] == "right_contains_left"


def test_date_interval_containment():
    rules = [
        rule("R-year", date_from="2025-01-01", date_to="2025-12-31"),
        rule("R-june", date_from="2025-06-01", date_to="2025-06-30"),
    ]
    (pair,) = post(rules)["pairs"]
    assert pair["relation"] == "left_contains_right"
    # 日期取交集内最早一天
    assert pair["witness"] == "01" + GTIN + "17" + "250601"


def test_partial_overlap_dates():
    rules = [
        rule("R1", date_from="2025-01-01", date_to="2025-12-31"),
        rule("R2", date_from="2025-06-15", date_to="2026-06-30"),
    ]
    (pair,) = post(rules)["pairs"]
    assert pair["relation"] == "partial_overlap"
    assert pair["witness"] == "01" + GTIN + "17" + "250615"
    assert check_matches(pair["witness"], rules) == ["R1", "R2"]


def test_partial_overlap_mixed_dimensions():
    # 一个规则约束批号、另一个约束序列号:互不包含但有交集
    rules = [rule("R-batch", batch="LOT1"), rule("R-serial", serial="SN9")]
    (pair,) = post(rules)["pairs"]
    assert pair["relation"] == "partial_overlap"
    # 变长字段后仍有 AI 时加 FNC1
    assert pair["witness"] == "01" + GTIN + "10" + "LOT1" + FNC1 + "21" + "SN9"
    assert check_matches(pair["witness"], rules) == ["R-batch", "R-serial"]


# ---------- 无交集不返回 ----------

def test_disjoint_pairs_not_returned():
    rules = [
        rule("R-g1", gtin=GTIN),
        rule("R-g2", gtin=GTIN2),  # GTIN 不同
        rule("R-lot1", batch="LOT1"),
        rule("R-lot2", batch="LOT2"),  # 批号不同
        rule("R-h1", date_from="2025-01-01", date_to="2025-06-30"),
        rule("R-h2", date_from="2025-07-01", date_to="2025-12-31"),  # 区间不相交
    ]
    body = post(rules)
    by_ids = {(p["left_id"], p["right_id"]) for p in body["pairs"]}
    assert ("R-g1", "R-g2") not in by_ids
    assert ("R-lot1", "R-lot2") not in by_ids
    assert ("R-h1", "R-h2") not in by_ids
    # 其余同 GTIN 的组合均有交集
    assert ("R-g1", "R-lot1") in by_ids
    assert ("R-lot1", "R-h1") in by_ids


# ---------- 证据扫描串 ----------

def test_witness_minimal_when_unconstrained():
    rules = [rule("R1"), rule("R2")]
    (pair,) = post(rules)["pairs"]
    assert pair["relation"] == "equivalent"
    assert pair["witness"] == "01" + GTIN  # 不含 17/10/21,也无需 FNC1


def test_witness_uses_constrained_side_only():
    rules = [rule("R1", serial="SN9"), rule("R2")]
    (pair,) = post(rules)["pairs"]
    assert pair["witness"] == "01" + GTIN + "21" + "SN9"
    assert check_matches(pair["witness"], rules) == ["R1", "R2"]


def test_witness_combines_all_constraints():
    rules = [
        rule("R1", batch="LOT1", date_from="2025-03-01", date_to="2025-12-31"),
        rule("R2", serial="SN9", date_from="2024-01-01", date_to="2025-05-31"),
    ]
    (pair,) = post(rules)["pairs"]
    assert pair["relation"] == "partial_overlap"
    # 日期取交集 [2025-03-01, 2025-05-31] 内最早一天;批号后有序列号 → FNC1
    assert pair["witness"] == (
        "01" + GTIN + "17" + "250301" + "10" + "LOT1" + FNC1 + "21" + "SN9"
    )
    assert check_matches(pair["witness"], rules) == ["R1", "R2"]


def test_all_pair_witnesses_hit_both_rules():
    rules = [
        rule("R0"),
        rule("R1", batch="LOT1"),
        rule("R2", serial="SN9", date_from="2025-06-01"),
        rule("R3", batch="LOT1", date_to="2025-12-31"),
        rule("R4", gtin=GTIN2),
    ]
    body = post(rules)
    assert body["pairs"], "应至少有一对"
    for pair in body["pairs"]:
        pair_rules = [rules[pair["left_index"]], rules[pair["right_index"]]]
        assert check_matches(pair["witness"], pair_rules) == [
            pair["left_id"],
            pair["right_id"],
        ], pair


# ---------- 排序与下标 ----------

def test_pairs_sorted_by_input_index():
    rules = [
        rule("R0", batch="LOT1"),
        rule("R1"),
        rule("R2", gtin=GTIN2),  # 与 0/1/3 均无交集
        rule("R3", serial="SN9"),
    ]
    body = post(rules)
    keys = [(p["left_index"], p["right_index"]) for p in body["pairs"]]
    assert keys == sorted(keys)
    assert keys == [(0, 1), (0, 3), (1, 3)]
    ids = [(p["left_id"], p["right_id"]) for p in body["pairs"]]
    assert ids == [("R0", "R1"), ("R0", "R3"), ("R1", "R3")]


# ---------- 请求体验证(422) ----------

def _expect_422(payload):
    resp = client.post("/v1/recall/rule-relations", json=payload)
    assert resp.status_code == 422, resp.text


def test_validation_rule_count():
    _expect_422({"rules": []})
    _expect_422({"rules": [rule("R1")]})  # 至少 2 条
    _expect_422({"rules": [rule(f"R{i}") for i in range(101)]})


def test_validation_rule_ids_unique():
    _expect_422({"rules": [rule("R1"), rule("R1")]})


def test_validation_rule_fields():
    _expect_422({"rules": [rule("R1", gtin="123"), rule("R2")]})
    bad = GTIN[:-1] + str((int(GTIN[-1]) + 1) % 10)
    _expect_422({"rules": [rule("R1", gtin=bad), rule("R2")]})
    _expect_422({"rules": [rule("R1", batch=""), rule("R2")]})
    _expect_422({"rules": [rule("R1", serial="A" * 21), rule("R2")]})
    _expect_422(
        {"rules": [rule("R1", date_from="2026-01-01", date_to="2025-01-01"), rule("R2")]}
    )
    _expect_422({"rules": [rule("R1", unknown_field=1), rule("R2")]})
    _expect_422({"rules": [rule("R1"), rule("R2")], "scans": []})
