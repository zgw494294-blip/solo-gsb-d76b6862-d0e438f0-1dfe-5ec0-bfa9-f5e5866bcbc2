"""POST /v1/custody/audit 的端到端测试。"""

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


def scan(gtin=GTIN, batch="LOT1", serial="SN1", expiry="251231"):
    """按 01/17/10/21 顺序拼接扫描串(变长字段后视需要补 FNC1)。"""
    s = "01" + gtin
    if expiry is not None:
        s += "17" + expiry
    if batch is not None:
        s += "10" + batch
        if serial is not None:
            s += FNC1
    if serial is not None:
        s += "21" + serial
    return s


def event(scan_str, occurred_at, to_location, from_location=None):
    return {
        "scan": scan_str,
        "occurred_at": occurred_at,
        "from_location": from_location,
        "to_location": to_location,
    }


def post(events):
    resp = client.post("/v1/custody/audit", json={"events": events})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 正常路径 ----------

def test_chain_processed_in_utc_time_order():
    # 输入乱序;按时间升序重放后构成 null→A→B→C 的完整交接链
    events = [
        event(scan(), "2026-09-01T03:00:00Z", "WH-C", from_location="WH-B"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),
    ]
    body = post(events)
    assert body["errors"] == []
    assert body["items"] == [
        {"gtin": GTIN, "serial": "SN1", "events": [1, 2, 0], "holder": "WH-C"}
    ]


def test_occurred_at_normalized_across_timezones():
    # +08:00 的 09:00 即 UTC 01:00,落在 00:30Z 与 02:00Z 之间
    events = [
        event(scan(), "2026-09-01T09:00:00+08:00", "WH-B", from_location="WH-A"),
        event(scan(), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-B"),
        event(scan(), "2026-09-01T00:30:00Z", "WH-A"),
    ]
    body = post(events)
    assert body["errors"] == []
    assert body["items"][0]["events"] == [2, 0, 1]
    assert body["items"][0]["holder"] == "WH-C"


def test_same_instant_uses_input_order():
    # 同一时刻(不同时区写法)依输入顺序处理
    events = [
        event(scan(), "2026-09-01T08:00:00+08:00", "WH-A"),
        event(scan(), "2026-09-01T00:00:00Z", "WH-B", from_location="WH-A"),
    ]
    body = post(events)
    assert body["errors"] == []
    assert body["items"][0]["events"] == [0, 1]
    assert body["items"][0]["holder"] == "WH-B"
    # 输入顺序反过来,第一条就成了非法的首次事件
    body = post(list(reversed(events)))
    assert [item["events"] for item in body["items"]] == [[1]]
    assert body["items"][0]["holder"] == "WH-A"
    assert [e["index"] for e in body["errors"]] == [0]
    assert "首次" in body["errors"][0]["reason"]


def test_multiple_items_sorted_by_gtin_then_serial():
    events = [
        event(scan(gtin=GTIN2, serial="SN2"), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(gtin=GTIN, serial="SN2"), "2026-09-01T00:00:00Z", "WH-B"),
        event(scan(gtin=GTIN, serial="SN1"), "2026-09-01T00:00:00Z", "WH-C"),
    ]
    body = post(events)
    assert body["errors"] == []
    assert [(i["gtin"], i["serial"]) for i in body["items"]] == [
        (GTIN, "SN1"),
        (GTIN, "SN2"),
        (GTIN2, "SN2"),
    ]
    assert [i["holder"] for i in body["items"]] == ["WH-C", "WH-B", "WH-A"]


def test_batch_and_expiry_absent_on_all_events_is_consistent():
    # 首次扫描无批号 / 有效期,后续同样缺失 → 一致
    events = [
        event(scan(batch=None, expiry=None), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(batch=None, expiry=None), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
    ]
    body = post(events)
    assert body["errors"] == []
    assert body["items"][0]["holder"] == "WH-B"


# ---------- 交接链约束 ----------

def test_first_event_must_have_null_from_location():
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A", from_location="WH-X"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B"),  # 上一条失败,本条仍是首次事件
    ]
    body = post(events)
    assert body["items"] == [{"gtin": GTIN, "serial": "SN1", "events": [1], "holder": "WH-B"}]
    assert len(body["errors"]) == 1
    assert body["errors"][0]["index"] == 0
    assert "首次" in body["errors"][0]["reason"]
    assert "null" in body["errors"][0]["reason"]


def test_from_location_must_equal_last_successful_to_location():
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-C", from_location="WH-X"),  # 链断裂
        event(scan(), "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),  # 接上次成功事件
    ]
    body = post(events)
    assert body["items"][0]["events"] == [0, 2]
    assert body["items"][0]["holder"] == "WH-B"  # 失败事件不改变持有人
    assert [e["index"] for e in body["errors"]] == [1]
    assert "WH-A" in body["errors"][0]["reason"]


def test_from_location_null_on_followup_event_fails():
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B"),  # 后续事件 from_location 为 null
    ]
    body = post(events)
    assert body["items"][0]["events"] == [0]
    assert body["items"][0]["holder"] == "WH-A"
    assert [e["index"] for e in body["errors"]] == [1]


# ---------- 批号 / 有效期冲突 ----------

def test_batch_conflict_does_not_change_holder():
    events = [
        event(scan(batch="LOT1"), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(batch="LOT2"), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        event(scan(batch="LOT1"), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-A"),
    ]
    body = post(events)
    assert body["items"][0]["events"] == [0, 2]
    assert body["items"][0]["holder"] == "WH-C"
    assert [e["index"] for e in body["errors"]] == [1]
    assert "批号" in body["errors"][0]["reason"]


def test_batch_missing_on_followup_conflicts():
    events = [
        event(scan(batch="LOT1"), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(batch=None), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
    ]
    body = post(events)
    assert body["items"][0]["events"] == [0]
    assert [e["index"] for e in body["errors"]] == [1]
    assert "批号" in body["errors"][0]["reason"]


def test_expiry_conflict_and_missing():
    events = [
        event(scan(expiry="251231"), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(expiry="260101"), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        event(scan(expiry=None), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-A"),
    ]
    body = post(events)
    assert body["items"][0]["events"] == [0]
    assert body["items"][0]["holder"] == "WH-A"
    assert [e["index"] for e in body["errors"]] == [1, 2]
    assert all("有效期" in e["reason"] for e in body["errors"])


# ---------- 扫描问题 ----------

def test_missing_serial_reported_and_skipped():
    events = [
        event(scan(serial=None), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B"),
    ]
    body = post(events)
    assert body["items"] == [{"gtin": GTIN, "serial": "SN1", "events": [1], "holder": "WH-B"}]
    assert [e["index"] for e in body["errors"]] == [0]
    assert "序列号" in body["errors"][0]["reason"]


def test_invalid_scan_reported_and_skipped():
    events = [
        event("01" + GTIN + "99" + "AB", "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B"),
    ]
    body = post(events)
    assert body["items"][0]["events"] == [1]
    assert [e["index"] for e in body["errors"]] == [0]
    assert "未知 AI" in body["errors"][0]["reason"]


def test_errors_sorted_by_original_index():
    events = [
        event(scan(serial=None), "2026-09-01T02:00:00Z", "WH-A"),  # 下标 0,时间最晚
        event("01" + GTIN + "99", "2026-09-01T01:00:00Z", "WH-B"),  # 下标 1
        event(scan(), "2026-09-01T00:00:00Z", "WH-C", from_location="WH-X"),  # 下标 2,时间最早
    ]
    body = post(events)
    assert body["items"] == []
    assert [e["index"] for e in body["errors"]] == [0, 1, 2]


# ---------- 请求体验证(422) ----------

def _expect_422(payload):
    resp = client.post("/v1/custody/audit", json=payload)
    assert resp.status_code == 422, resp.text


def test_validation_event_count():
    _expect_422({"events": []})
    _expect_422({"events": [event(scan(), "2026-09-01T00:00:00Z", "WH-A")] * 301})
    resp = client.post(
        "/v1/custody/audit",
        json={"events": [event(scan(), "2026-09-01T00:00:00Z", "WH-A")] * 300},
    )
    assert resp.status_code == 200, resp.text


def test_validation_occurred_at_must_have_timezone():
    _expect_422({"events": [event(scan(), "2026-09-01T00:00:00", "WH-A")]})
    _expect_422({"events": [event(scan(), "not-a-time", "WH-A")]})


def test_validation_to_location_must_be_non_empty():
    _expect_422({"events": [event(scan(), "2026-09-01T00:00:00Z", "")]})


def test_validation_required_fields_and_no_extra():
    _expect_422({"events": [{"occurred_at": "2026-09-01T00:00:00Z", "from_location": None, "to_location": "A"}]})
    _expect_422({"events": [{"scan": scan(), "from_location": None, "to_location": "A"}]})
    _expect_422({"events": [{"scan": scan(), "occurred_at": "2026-09-01T00:00:00Z", "to_location": "A"}]})
    _expect_422({"events": [dict(event(scan(), "2026-09-01T00:00:00Z", "A"), extra=1)]})
