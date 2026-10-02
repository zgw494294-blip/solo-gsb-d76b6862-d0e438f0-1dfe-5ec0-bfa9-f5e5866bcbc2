"""POST /v1/receiving/reconcile 的端到端测试。"""

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


def scan(gtin=GTIN, serial=None, batch=None, expiry=None, prefix=False, raw=None):
    """拼接扫描串;raw 直接透传非法串。变长字段后视需要补 FNC1。"""
    if raw is not None:
        return raw
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


def line(line_id, gtin=GTIN, quantity=1, batch=None, serial=None, expiry=None):
    body = {"id": line_id, "gtin": gtin, "quantity": quantity}
    if batch is not None:
        body["batch"] = batch
    if serial is not None:
        body["serial"] = serial
    if expiry is not None:
        body["expiry"] = expiry
    return body


def post(scans, lines, expected=200):
    resp = client.post(
        "/v1/receiving/reconcile", json={"scans": scans, "lines": lines}
    )
    assert resp.status_code == expected, resp.text
    return resp.json() if expected == 200 else resp


def assigned(body):
    return [a["line_id"] for a in body["assignments"]]


# ---------- 基本核销 / 缺额 / 多余 ----------


def test_exact_match_consumes_all_quantity():
    body = post([scan(serial="SN1"), scan(serial="SN2")], [line("L1", quantity=2)])
    assert assigned(body) == ["L1", "L1"]
    assert body["unassigned_indices"] == []
    assert body["duplicate_indices"] == []
    assert body["errors"] == []
    assert body["lines"] == [{"id": "L1", "remaining": 0}]


def test_shortage_and_surplus():
    body = post(
        [scan(serial="SN1"), scan(serial="SN2"), scan(gtin=GTIN2, serial="X")],
        [line("L1", quantity=3)],
    )
    assert assigned(body) == ["L1", "L1", None]
    assert body["unassigned_indices"] == [2]
    assert body["lines"] == [{"id": "L1", "remaining": 1}]


def test_same_non_serial_scans_fill_multiple_slots():
    # 无 AI 21 的扫描不做实物去重,三条同内容扫描可占三个名额
    body = post([scan(), scan(), scan()], [line("L1", quantity=3)])
    assert assigned(body) == ["L1", "L1", "L1"]
    assert body["lines"][0]["remaining"] == 0


def test_capacity_leaves_one_surplus_scan():
    body = post([scan(), scan(), scan()], [line("L1", quantity=2)])
    assert assigned(body) == ["L1", "L1", None]
    assert body["unassigned_indices"] == [2]
    assert body["lines"][0]["remaining"] == 0


# ---------- 解析失败 ----------


def test_parse_error_reports_offset_and_reason_and_is_neither_assigned_nor_surplus():
    bad = "010950600112345499"  # 校验位错误的 GTIN + 未知内容
    body = post([bad, scan()], [line("L1", quantity=2)])
    assert assigned(body) == [None, "L1"]
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert error["index"] == 0
    assert error["offset"] >= 0
    assert error["reason"]  # 非空原因
    assert body["unassigned_indices"] == []  # 非法扫描不算多余扫描


def test_parse_error_offset_includes_symbol_prefix():
    bad = "]d201123"  # ]d2 后 GTIN 长度不足
    body = post([bad], [line("L1", quantity=1)])
    error = body["errors"][0]
    assert error["index"] == 0
    assert error["offset"] >= 3


# ---------- AI 21 同 GTIN+序列号去重 ----------


def test_duplicate_serial_scanned_thrice():
    body = post(
        [scan(serial="SN1", batch="A"), scan(serial="SN1", batch="B"), scan(serial="SN1")],
        [line("L1", quantity=5)],
    )
    assert assigned(body) == ["L1", None, None]
    assert body["duplicate_indices"] == [1, 2]
    assert body["unassigned_indices"] == []
    assert body["errors"] == []
    assert body["lines"][0]["remaining"] == 4


def test_duplicate_serial_even_when_first_cannot_be_assigned():
    # 首次扫描没有任何兼容行时仍是首次;后续同 GTIN+序列号照样算重复
    body = post([scan(serial="SN1"), scan(serial="SN1")], [line("L1", gtin=GTIN2, quantity=1)])
    assert assigned(body) == [None, None]
    assert body["duplicate_indices"] == [1]
    assert body["unassigned_indices"] == [0]


def test_distinct_serials_both_fill_gtin_only_line():
    body = post([scan(serial="SN1"), scan(serial="SN2")], [line("L1", quantity=2)])
    assert assigned(body) == ["L1", "L1"]


# ---------- 限定字段匹配 ----------


def test_batch_constraint_requires_present_and_equal():
    body = post(
        [scan(batch="LOT1"), scan(batch="LOT2"), scan()],
        [line("L1", quantity=3, batch="LOT1")],
    )
    assert assigned(body) == ["L1", None, None]
    assert body["unassigned_indices"] == [1, 2]


def test_serial_constraint_requires_present_and_equal():
    body = post(
        [scan(serial="SN9"), scan(serial="SN8"), scan(batch="B")],
        [line("L1", quantity=3, serial="SN9")],
    )
    assert assigned(body) == ["L1", None, None]
    assert body["unassigned_indices"] == [1, 2]


def test_expiry_constraint_requires_present_and_equal():
    body = post(
        [scan(expiry="260630"), scan(expiry="251231"), scan()],
        [line("L1", quantity=3, expiry="2026-06-30")],
    )
    assert assigned(body) == ["L1", None, None]


def test_expiry_day_zero_normalizes_to_month_end():
    # AI 17 日 00 => 当月末日:260600 解析为 2026-06-30
    body = post([scan(expiry="260600")], [line("L1", expiry="2026-06-30")])
    assert assigned(body) == ["L1"]


def test_extra_scan_fields_are_allowed():
    # 期望行只限定 GTIN 时,扫描多带批号 / 序列号 / 日期仍可占用
    body = post([scan(serial="SN1", batch="B", expiry="260101")], [line("L1")])
    assert assigned(body) == ["L1"]


def test_all_three_constraints_must_match():
    body = post(
        [
            scan(serial="SN1", batch="B1", expiry="260101"),
            scan(serial="SN2", batch="B1", expiry="260102"),
        ],
        [line("L1", serial="SN1", batch="B1", expiry="2026-01-01", quantity=2)],
    )
    assert assigned(body) == ["L1", None]
    assert body["unassigned_indices"] == [1]


# ---------- 全局分配目标 ----------


def test_prefer_more_specific_line_when_f_tied():
    # 同一条扫描对两行都兼容;F 都为 1,W 优先:限定字段多的行得核销
    body = post(
        [scan(batch="LOT1")],
        [line("L-generic"), line("L-lot", batch="LOT1")],
    )
    assert assigned(body) == ["L-lot"]


def test_maximize_total_writeoffs_over_specificity():
    # s0 对两行兼容,s1 只兼容泛化行;F=2 要求 s0 占用限定行、s1 占用泛化行
    body = post(
        [scan(batch="LOT1", serial="A"), scan(batch="LOT2", serial="B")],
        [
            line("L-lot", quantity=1, batch="LOT1"),
            line("L-generic", quantity=1),
        ],
    )
    assert assigned(body) == ["L-lot", "L-generic"]


def test_specificity_tiebreak_across_two_scans():
    # L-lot 名额 1、L-generic 名额 2;两条扫描都可核销时,W 优先把 LOT1 扫描给限定行
    body = post(
        [scan(batch="LOT1", serial="A"), scan(batch="LOT2", serial="B")],
        [
            line("L-lot", quantity=1, batch="LOT1"),
            line("L-generic", quantity=2),
        ],
    )
    assert assigned(body) == ["L-lot", "L-generic"]


def test_lex_tiebreak_prefers_earlier_line_for_first_scan():
    body = post([scan()], [line("L0"), line("L1")])
    assert assigned(body) == ["L0"]


def test_lex_tiebreak_single_slot_goes_to_earlier_scan():
    body = post([scan(serial="A"), scan(serial="B")], [line("L0", quantity=1)])
    assert assigned(body) == ["L0", None]
    assert body["unassigned_indices"] == [1]


def test_lex_tiebreak_order_preserved_across_scans():
    body = post([scan(serial="A"), scan(serial="B")], [line("L0"), line("L1")])
    assert assigned(body) == ["L0", "L1"]


def test_global_feasibility_forces_cross_assignment():
    # s0 只能去 L1,s1 两行兼容;F=2 必须 s0→L1、s1→L0
    body = post(
        [scan(batch="LOT1", serial="A"), scan(batch="LOT9", serial="B")],
        [line("L0"), line("L1", batch="LOT1")],
    )
    assert assigned(body) == ["L1", "L0"]


# ---------- 响应结构与顺序 ----------


def test_assignments_keep_input_order_and_lines_keep_input_order():
    body = post(
        [
            scan(gtin=GTIN3, serial="A"),  # 0 多余:无对应 GTIN 行
            "0100",  # 1 解析失败
            scan(gtin=GTIN2, serial="B"),  # 2 -> L2
            scan(gtin=GTIN2, serial="B"),  # 3 重复
            scan(gtin=GTIN2, serial="C"),  # 4 多余(L2 仅 1 个名额)
        ],
        [line("L1", gtin=GTIN, quantity=1), line("L2", gtin=GTIN2, quantity=1)],
    )
    assert [a["index"] for a in body["assignments"]] == [0, 1, 2, 3, 4]
    assert assigned(body) == [None, None, "L2", None, None]
    assert [e["index"] for e in body["errors"]] == [1]
    assert body["unassigned_indices"] == [0, 4]
    assert body["duplicate_indices"] == [3]
    assert body["lines"] == [
        {"id": "L1", "remaining": 1},
        {"id": "L2", "remaining": 0},
    ]


# ---------- 422 非法期望行 / 请求 ----------


def post422(scans, lines):
    return post(scans, lines, expected=422)


def test_invalid_gtin_check_digit_rejected():
    post422([scan()], [line("L1", gtin="00000000000001")])  # 校验位错


def test_quantity_must_be_positive_integer():
    post422([scan()], [line("L1", quantity=0)])


def test_quantity_bool_rejected():
    resp = client.post(
        "/v1/receiving/reconcile",
        json={"scans": [scan()], "lines": [{"id": "L1", "gtin": GTIN, "quantity": True}]},
    )
    assert resp.status_code == 422


def test_duplicate_line_ids_rejected():
    post422([scan()], [line("L1"), line("L1", gtin=GTIN2)])


def test_empty_id_rejected():
    post422([scan()], [line("")])


def test_empty_batch_rejected():
    post422([scan()], [line("L1", batch="")])


def test_expiry_out_of_supported_years_rejected():
    post422([scan()], [line("L1", expiry="1999-12-31")])
    post422([scan()], [line("L1", expiry="2100-01-01")])


def test_total_quantity_over_60_rejected():
    post422([scan()], [line("L1", quantity=31), line("L2", gtin=GTIN2, quantity=30)])


def test_total_quantity_exactly_60_accepted():
    post([scan(serial="A")], [line("L1", quantity=60)])


def test_scan_count_limits():
    post422([], [line("L1")])
    post422([scan()] * 61, [line("L1", quantity=60)])


def test_line_count_limits():
    post422([scan()], [])
    post422([scan()], [line(f"L{i}", quantity=1) for i in range(61)])


def test_extra_and_missing_fields_rejected():
    resp = client.post(
        "/v1/receiving/reconcile",
        json={"scans": [scan()], "lines": [line("L1")], "nope": 1},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/v1/receiving/reconcile",
        json={
            "scans": [scan()],
            "lines": [{"id": "L1", "gtin": GTIN, "quantity": 1, "what": "x"}],
        },
    )
    assert resp.status_code == 422
