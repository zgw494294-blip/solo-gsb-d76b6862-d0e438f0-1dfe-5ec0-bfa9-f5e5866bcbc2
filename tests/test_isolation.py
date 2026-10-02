"""POST /v1/recall/isolation-plan 的端到端测试。"""

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
GTIN3 = make_gtin("0950600112347")
GTIN4 = make_gtin("0950600112348")


def scan(gtin=GTIN, serial="SN1", batch=None, expiry=None, prefix=False):
    """拼接含 AI 21 的扫描串(变长字段后视需要补 FNC1)。"""
    s = "]d2" if prefix else ""
    s += "01" + gtin
    if expiry is not None:
        s += "17" + expiry
    if batch is not None:
        s += "10" + batch
        if serial is not None:
            s += FNC1
    if serial is not None:
        s += "21" + serial
    return s


def rule(rid, gtin=GTIN, **kw):
    return {"id": rid, "gtin": gtin, **kw}


def pack(parent, child):
    return {"action": "pack", "parent_scan": parent, "child_scan": child}


def unpack(parent, child):
    return {"action": "unpack", "parent_scan": parent, "child_scan": child}


def post(scans, rules, operations, sealed_scans=()):
    resp = client.post(
        "/v1/recall/isolation-plan",
        json={
            "scans": scans,
            "rules": rules,
            "operations": operations,
            "sealed_scans": list(sealed_scans),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def unit_map(body):
    return {(u["gtin"], u["serial"]): u for u in body["units"]}


# ---------- 基本归并:无包装 / 无封签 ----------

def test_hit_without_packing_or_sealed_yields_self_unit():
    body = post([scan(GTIN, "A")], [rule("R1")], [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))])
    assert body["errors"] == []
    assert body["operation_errors"] == []
    assert body["units"] == [
        {"gtin": GTIN, "serial": "A", "scan_indices": [0], "rule_ids": ["R1"]}
    ]


def test_miss_produces_no_unit():
    body = post(
        [scan(GTIN, "A"), scan(GTIN2, "B")],
        [rule("R1", gtin=GTIN3)],  # 谁也不命中
        [pack(scan(GTIN3, "P"), scan(GTIN4, "C"))],
    )
    assert body["units"] == []
    assert body["errors"] == []


def test_rule_fields_reused_for_matching():
    # 批号 / 日期区间等规则条件与召回核验一致
    s = scan(GTIN, "A", batch="LOT1", expiry="251231")
    body = post(
        [s],
        [
            rule("R-batch", batch="LOT2"),  # 批号不符
            rule("R-date", date_from="2026-01-01"),  # 日期早于区间
            rule("R-hit", batch="LOT1", date_to="2025-12-31"),
        ],
        [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))],
    )
    assert body["units"][0]["rule_ids"] == ["R-hit"]


# ---------- 沿父链选最高层已封签祖先 ----------

def test_highest_sealed_ancestor_becomes_unit():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3)],
        [pack(pallet, case), pack(case, item)],
        sealed_scans=[pallet],
    )
    assert body["units"] == [
        {"gtin": GTIN, "serial": "P", "scan_indices": [0], "rule_ids": ["R1"]}
    ]


def test_middle_sealed_ancestor_wins_over_unsealed_root():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3)],
        [pack(pallet, case), pack(case, item)],
        sealed_scans=[case],  # 只有中间层封签
    )
    assert list(unit_map(body)) == [(GTIN2, "C")]


def test_highest_of_multiple_sealed_ancestors():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3)],
        [pack(pallet, case), pack(case, item)],
        sealed_scans=[case, pallet],  # 两层都封签,取最高层
    )
    assert list(unit_map(body)) == [(GTIN, "P")]


def test_sealed_item_itself_is_unit_even_under_unsealed_parent():
    case, item = scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3)],
        [pack(case, item)],
        sealed_scans=[item],  # 命中实物自身已封签
    )
    assert list(unit_map(body)) == [(GTIN3, "I")]


def test_no_sealed_ancestor_falls_back_to_item_itself():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3)],
        [pack(pallet, case), pack(case, item)],
        sealed_scans=[scan(GTIN4, "OTHER")],  # 封签的是无关实物
    )
    assert list(unit_map(body)) == [(GTIN3, "I")]


def test_final_parent_chain_after_unpack():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3)],
        [pack(pallet, case), pack(case, item), unpack(case, item)],  # 最终 I 已取出
        sealed_scans=[pallet, case],
    )
    # 最终父链上 I 无父件,也没有已封签祖先 → 单元为 I 自身
    assert list(unit_map(body)) == [(GTIN3, "I")]


# ---------- 单元合并与排序 ----------

def test_two_items_merge_into_one_sealed_unit():
    case = scan(GTIN, "C")
    i1, i2 = scan(GTIN2, "I1"), scan(GTIN3, "I2")
    body = post(
        [i1, i2],
        [rule("R1", gtin=GTIN2), rule("R2", gtin=GTIN3)],
        [pack(case, i1), pack(case, i2)],
        sealed_scans=[case],
    )
    assert body["units"] == [
        {"gtin": GTIN, "serial": "C", "scan_indices": [0, 1], "rule_ids": ["R1", "R2"]}
    ]


def test_rule_ids_deduped_in_rule_input_order():
    case = scan(GTIN, "C")
    i1, i2 = scan(GTIN2, "I1"), scan(GTIN3, "I2")
    rules = [
        rule("R1", gtin=GTIN2),
        rule("R2", gtin=GTIN2),  # 与 R1 同时命中 I1
        rule("R3", gtin=GTIN3),
        rule("R4", gtin=GTIN4),  # 未命中
    ]
    # 扫描顺序故意与规则顺序交错:R3 的实物排在前面
    body = post([i2, i1], rules, [pack(case, i1), pack(case, i2)], sealed_scans=[case])
    (unit,) = body["units"]
    assert unit["scan_indices"] == [0, 1]
    assert unit["rule_ids"] == ["R1", "R2", "R3"]  # 去重后保持规则输入顺序


def test_units_sorted_by_gtin_then_serial():
    body = post(
        [scan(GTIN2, "B"), scan(GTIN, "Z"), scan(GTIN, "A")],
        [rule("R1"), rule("R2", gtin=GTIN2)],
        [pack(scan(GTIN3, "P"), scan(GTIN4, "C"))],
    )
    assert [(u["gtin"], u["serial"]) for u in body["units"]] == [
        (GTIN, "A"),
        (GTIN, "Z"),
        (GTIN2, "B"),
    ]


def test_scan_indices_sorted_ascending():
    case = scan(GTIN, "C")
    i1, i2 = scan(GTIN2, "I1"), scan(GTIN2, "I2")
    body = post(
        [i2, scan(GTIN3, "MISS"), i1],
        [rule("R1", gtin=GTIN2)],
        [pack(case, i1), pack(case, i2)],
        sealed_scans=[case],
    )
    (unit,) = body["units"]
    assert unit["scan_indices"] == [0, 2]


# ---------- 库存扫描错误与 422 ----------

def test_invalid_and_missing_serial_scans_reported_by_index_and_skipped():
    good = scan(GTIN, "A")
    no_serial = "01" + GTIN2 + "17251231"
    bad = "01" + GTIN3 + "99"  # 未知 AI
    body = post([bad, good, no_serial], [rule("R1")], [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))])
    assert [e["index"] for e in body["errors"]] == [0, 2]
    assert "扫描非法" in body["errors"][0]["reason"]
    assert "缺少 AI 21" in body["errors"][1]["reason"]
    # 合法扫描照常匹配
    assert body["units"] == [
        {"gtin": GTIN, "serial": "A", "scan_indices": [1], "rule_ids": ["R1"]}
    ]


def test_duplicate_valid_inventory_scan_returns_422():
    resp = client.post(
        "/v1/recall/isolation-plan",
        json={
            "scans": [scan(GTIN, "A"), scan(GTIN2, "B"), scan(GTIN, "A", batch="L1")],
            "rules": [rule("R1")],
            "operations": [pack(scan(GTIN3, "P"), scan(GTIN4, "C"))],
            "sealed_scans": [],
        },
    )
    assert resp.status_code == 422
    assert "重复" in resp.json()["detail"]


def test_duplicate_identity_ignores_other_ais():
    # 批号不同但 GTIN+序列号相同 → 同一实物,仍属重复
    resp = client.post(
        "/v1/recall/isolation-plan",
        json={
            "scans": [scan(GTIN, "A", batch="L1"), scan(GTIN, "A", batch="L2")],
            "rules": [rule("R1")],
            "operations": [pack(scan(GTIN3, "P"), scan(GTIN4, "C"))],
            "sealed_scans": [],
        },
    )
    assert resp.status_code == 422


def test_failed_inventory_scans_do_not_trigger_duplicate_422():
    # 非法扫描没有实物身份,两条同样的非法扫描只各记一条错误
    bad = "01" + GTIN + "99"
    body = post([bad, bad], [rule("R1")], [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))])
    assert [e["index"] for e in body["errors"]] == [0, 1]


# ---------- 封签扫描 422 ----------

def _expect_422(payload):
    resp = client.post("/v1/recall/isolation-plan", json=payload)
    assert resp.status_code == 422, resp.text
    return resp


def _base_payload(**kw):
    payload = {
        "scans": [scan(GTIN, "A")],
        "rules": [rule("R1")],
        "operations": [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))],
        "sealed_scans": [],
    }
    payload.update(kw)
    return payload


def test_sealed_scan_invalid_returns_422():
    resp = _expect_422(_base_payload(sealed_scans=["01" + GTIN + "99"]))
    assert "封签扫描 0 非法" in resp.json()["detail"]


def test_sealed_scan_missing_serial_returns_422():
    resp = _expect_422(_base_payload(sealed_scans=["01" + GTIN + "17251231"]))
    assert "缺少 AI 21" in resp.json()["detail"]


def test_sealed_scan_duplicate_returns_422():
    resp = _expect_422(
        _base_payload(sealed_scans=[scan(GTIN, "P"), scan(GTIN2, "Q"), scan(GTIN, "P")])
    )
    assert "重复" in resp.json()["detail"]


def test_sealed_scan_may_repeat_inventory_scan():
    # 封签集合与库存扫描是不同输入,同一实物可同时出现
    body = post([scan(GTIN, "A")], [rule("R1")],
                [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))],
                sealed_scans=[scan(GTIN, "A")])
    assert body["units"] == [
        {"gtin": GTIN, "serial": "A", "scan_indices": [0], "rule_ids": ["R1"]}
    ]


# ---------- 装拆操作重放 ----------

def test_failed_operations_keep_relations_and_report_index():
    p1, p2, c = scan(GTIN, "P1"), scan(GTIN2, "P2"), scan(GTIN3, "C")
    body = post(
        [c],
        [rule("R1", gtin=GTIN3)],
        [
            pack(p1, c),
            pack(p2, c),  # C 已有父件 → 失败,不改变关系
            unpack(p2, c),  # P2 不是当前父件 → 失败
        ],
        sealed_scans=[p1],
    )
    assert [e["index"] for e in body["operation_errors"]] == [1, 2]
    # 关系未被失败操作改变:C 的父件仍是已封签的 P1
    assert list(unit_map(body)) == [(GTIN, "P1")]


def test_invalid_operation_scan_reported_in_operation_errors():
    c = scan(GTIN3, "C")
    body = post(
        [c],
        [rule("R1", gtin=GTIN3)],
        [pack("01" + GTIN + "99", c), pack(scan(GTIN, "P"), c)],
        sealed_scans=[scan(GTIN, "P")],
    )
    assert [e["index"] for e in body["operation_errors"]] == [0]
    assert "父件扫描非法" in body["operation_errors"][0]["reason"]
    assert list(unit_map(body)) == [(GTIN, "P")]


def test_hit_item_not_in_any_operation_uses_self():
    body = post(
        [scan(GTIN, "A")],
        [rule("R1")],
        [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))],  # 与 A 无关的操作
        sealed_scans=[scan(GTIN2, "P")],
    )
    assert list(unit_map(body)) == [(GTIN, "A")]


# ---------- 请求体验证(422) ----------

def test_validation_counts():
    op = pack(scan(GTIN2, "P"), scan(GTIN3, "C"))
    _expect_422(_base_payload(scans=[]))
    _expect_422(_base_payload(scans=[scan(GTIN, "A")] * 101))
    _expect_422(_base_payload(rules=[]))
    _expect_422(_base_payload(rules=[rule("R1")] * 101))
    _expect_422(_base_payload(operations=[]))
    _expect_422(_base_payload(operations=[op] * 301))
    _expect_422(_base_payload(sealed_scans=[scan(GTIN4, "S")] * 301))
    # 边界:1 / 100 / 300 / 300 均合法
    body = post(
        [scan(GTIN, "A")],
        [rule("R1")],
        [op],
        sealed_scans=[scan(GTIN4, f"S{i}") for i in range(300)],
    )
    assert body["errors"] == []


def test_validation_rule_ids_unique_and_rule_fields():
    _expect_422(_base_payload(rules=[rule("R1"), rule("R1")]))
    _expect_422(_base_payload(rules=[rule("R1", gtin="09506001123450")]))  # 校验位错误


def test_validation_required_fields_and_no_extra():
    _expect_422({"scans": [scan(GTIN, "A")], "rules": [rule("R1")],
                 "operations": [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))]})  # 缺 sealed_scans
    _expect_422(_base_payload(extra=1))
    _expect_422(_base_payload(operations=[{"action": "move",
                                           "parent_scan": scan(GTIN2, "P"),
                                           "child_scan": scan(GTIN3, "C")}]))


def test_sealed_scans_empty_allowed():
    body = post([scan(GTIN, "A")], [rule("R1")],
                [pack(scan(GTIN2, "P"), scan(GTIN3, "C"))])
    assert body["units"][0]["gtin"] == GTIN


# ---------- 旧接口回归 ----------

def test_existing_endpoints_still_available():
    assert client.get("/healthz").json() == {"status": "ok"}
    resp = client.post(
        "/v1/recall/check",
        json={"scans": [scan(GTIN, "A")], "rules": [rule("R1")]},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["matches"] == ["R1"]
    resp = client.post(
        "/v1/packing/audit",
        json={"operations": [pack(scan(GTIN, "P"), scan(GTIN2, "C"))]},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2
