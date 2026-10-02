"""POST /v1/custody/trim 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FNC1 = "\x1d"  # ASCII 29


def make_gtin(body13: str) -> str:
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")


def scan(gtin=GTIN, batch="LOT1", serial="SN1", expiry="251231"):
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


def post(events, locked_indices=None):
    payload = {"events": events}
    if locked_indices is not None:
        payload["locked_indices"] = locked_indices
    return client.post("/v1/custody/trim", json=payload)


def trim(events, locked_indices=None):
    resp = post(events, locked_indices)
    assert resp.status_code == 200, resp.text
    return resp.json()


def expect_422(events, locked_indices=None):
    resp = post(events, locked_indices)
    assert resp.status_code == 422, resp.text
    return resp.json()


# ---------- 基本剔除:最少总数 ----------

def test_valid_chain_all_kept():
    events = [
        event(scan(), "2026-09-01T01:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),
        event(scan(), "2026-09-01T03:00:00Z", "WH-C", from_location="WH-B"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [0, 1, 2]
    assert body["removed_indices"] == []
    assert body["items"] == [{"gtin": GTIN, "serial": "SN1", "holder": "WH-C"}]


def test_invalid_and_missing_serial_forced_removed():
    events = [
        event(scan(serial=None), "2026-09-01T00:00:00Z", "WH-A"),
        event("01" + GTIN + "99AB", "2026-09-01T01:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T02:00:00Z", "WH-A"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [2]
    assert body["removed_indices"] == [0, 1]
    assert body["items"][0]["holder"] == "WH-A"


def test_broken_first_link_picks_lex_min_when_single_keep_tie():
    # 两条都可作为合法首条(from 均为 null),但无法互相接续;最多保留 1 条,
    # 并列时保留原下标序列字典序最小 => 下标 0。
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B"),  # from null,接不上 WH-A
    ]
    body = trim(events)
    assert body["kept_indices"] == [0]
    assert body["removed_indices"] == [1]
    assert body["items"][0]["holder"] == "WH-A"


def test_skip_broken_link_to_maximize_kept():
    # 0:null->A,1:A->X(合法),2:from X 但 to 无关;这里制造断裂再恢复
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-Z"),  # 断裂
        event(scan(), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-A"),  # 接 0
    ]
    body = trim(events)
    assert body["kept_indices"] == [0, 2]
    assert body["removed_indices"] == [1]
    assert body["items"][0]["holder"] == "WH-C"


def test_bad_first_event_skipped_to_keep_longer_tail():
    # 0 首次 from 非 null;1 才是 null 首次,2 接续 1 => 剔除 0,保留 1,2
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A", from_location="WH-X"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B"),
        event(scan(), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-B"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [1, 2]
    assert body["removed_indices"] == [0]


def test_batch_conflict_event_removed():
    events = [
        event(scan(batch="LOT1"), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(batch="LOT2"), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        event(scan(batch="LOT1"), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-A"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [0, 2]
    assert body["removed_indices"] == [1]
    assert body["items"][0]["holder"] == "WH-C"


def test_expiry_including_missing_must_match_first_kept():
    events = [
        event(scan(expiry=None), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(expiry="260101"), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        event(scan(expiry=None), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-A"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [0, 2]
    assert body["removed_indices"] == [1]


# ---------- 处理顺序:UTC 升序、同刻输入顺序 ----------

def test_processing_order_utc_then_input():
    # 输入乱序;保留结果按处理顺序给原下标
    events = [
        event(scan(), "2026-09-01T03:00:00Z", "WH-C", from_location="WH-B"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [1, 2, 0]
    assert body["removed_indices"] == []
    assert body["items"][0]["holder"] == "WH-C"


def test_same_instant_input_order_decides_chain():
    events = [
        event(scan(), "2026-09-01T08:00:00+08:00", "WH-A"),
        event(scan(), "2026-09-01T00:00:00Z", "WH-B", from_location="WH-A"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [0, 1]
    # 反序:00:00Z 那条先处理,它 from=WH-A 不能做首条;另一条可保留 => 剔 1
    body = trim(list(reversed(events)))
    assert body["kept_indices"] == [1]
    assert body["removed_indices"] == [0]


# ---------- 多实物 ----------

def test_multiple_items_independent_and_sorted():
    events = [
        event(scan(gtin=GTIN2, serial="SN2"), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(gtin=GTIN, serial="SN2"), "2026-09-01T00:00:00Z", "WH-B"),
        event(scan(gtin=GTIN, serial="SN1"), "2026-09-01T00:00:00Z", "WH-C"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [0, 1, 2]
    assert [(i["gtin"], i["serial"]) for i in body["items"]] == [
        (GTIN, "SN1"),
        (GTIN, "SN2"),
        (GTIN2, "SN2"),
    ]
    assert [i["holder"] for i in body["items"]] == ["WH-C", "WH-B", "WH-A"]


def test_item_with_no_kept_event_not_listed():
    # 唯一一条事件是非法首次,剔除 => 该实物不列出
    events = [event(scan(), "2026-09-01T00:00:00Z", "WH-A", from_location="WH-X")]
    body = trim(events)
    assert body["kept_indices"] == []
    assert body["removed_indices"] == [0]
    assert body["items"] == []


def test_each_item_maximizes_its_own_count():
    # 实物 A 必须剔 1 条;实物 B 全保留
    events = [
        event(scan(gtin=GTIN, serial="A"), "2026-09-01T00:00:00Z", "X", from_location="BAD"),
        event(scan(gtin=GTIN2, serial="B"), "2026-09-01T00:00:00Z", "Y"),
        event(scan(gtin=GTIN2, serial="B"), "2026-09-01T01:00:00Z", "Z", from_location="Y"),
        event(scan(gtin=GTIN, serial="A"), "2026-09-01T02:00:00Z", "M"),
    ]
    body = trim(events)
    assert sorted(body["kept_indices"]) == [1, 2, 3]
    assert body["removed_indices"] == [0]


# ---------- 字典序并列(全局合并序列) ----------

def test_tie_lexicographic_merged_sequence():
    # 两条候选首条(不同实物,处理位置相邻),都只能保留 1 条;
    # 保留原下标更小者使合并序列字典序最小。
    events = [
        event(scan(gtin=GTIN, serial="A"), "2026-09-01T00:00:00Z", "X"),
        event(scan(gtin=GTIN2, serial="B"), "2026-09-01T00:00:00Z", "Y"),
    ]
    body = trim(events)
    # 两者都保留时共 2 条,并非并列;构造一个真正只能二选一的并列场景见下
    assert sorted(body["kept_indices"]) == [0, 1]


def test_tie_global_lex_not_per_item_lex():
    # 构造处理顺序与原下标交错:实物 A 在较早槽位的候选原下标较大,
    # 但实物 B 在其后槽位必须保留一个很小的原下标;
    # 全局字典序应优先让更早槽位拿到最小原下标。
    # pos0(orig 3): A 候选首条 null->m ; pos1(orig 0): B 合法首条(必留)
    # pos2(orig 1): A 候选首条 null->n ; B 不相关
    # A 的两个候选互斥(都是首条),取 pos0 还是 pos2 都只保留 1 条;
    # 取 pos0 -> 合并 [3,0];取 pos2(B 在 pos1) -> 合并 [0,1] => 后者更小。
    events = [
        # orig 0:B 首条,处理在中间(pos1):UTC 01:00
        event(scan(gtin=GTIN2, serial="B"), "2026-09-01T01:00:00Z", "Y"),
        # orig 1:A 首条候选,处理在最后(pos2):UTC 02:00
        event(scan(gtin=GTIN, serial="A"), "2026-09-01T02:00:00Z", "N"),
        # orig 2:无关非法,填位置避免下标含义混乱
        event(scan(serial=None), "2026-09-01T03:00:00Z", "Z"),
        # orig 3:A 首条候选,处理最早(pos0):UTC 00:00
        event(scan(gtin=GTIN, serial="A"), "2026-09-01T00:00:00Z", "M"),
    ]
    body = trim(events)
    assert body["kept_indices"] == [0, 1]  # 处理顺序:pos1(orig0) 再 pos2(orig1)
    assert body["removed_indices"] == [2, 3]


# ---------- 锁定事件 ----------

def test_locked_valid_event_kept():
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A", from_location="WH-X"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B"),
    ]
    # 未锁定时首条会被剔除(保留 1 条时取下标 0);锁定下标 1 后仍保留 1
    body = trim(events, [1])
    assert body["kept_indices"] == [1]
    assert body["removed_indices"] == [0]
    # 锁定下标 0:它是非法首条且是唯一可保留位置,无法满足 => 422
    expect_422(events, [0])


def test_locked_chain_forces_extra_removals():
    # 0:null->A,1:A->B,2:B->C 全链;再锁定 2(本身合法),正常全保留
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        event(scan(), "2026-09-01T02:00:00Z", "WH-C", from_location="WH-B"),
    ]
    body = trim(events, [2])
    assert body["kept_indices"] == [0, 1, 2]


def test_locked_incompatible_pair_returns_422():
    # 同一实物两个锁定首条候选批号不同 => 不能同链
    events = [
        event(scan(batch="LOT1"), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(batch="LOT2"), "2026-09-01T01:00:00Z", "WH-B"),
    ]
    expect_422(events, [0, 1])


def test_locked_broken_link_returns_422():
    # 锁定两条断裂事件,无法同时保留
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-C", from_location="WH-Z"),
    ]
    expect_422(events, [0, 1])


def test_locked_invalid_scan_returns_422():
    events = [
        event("01" + GTIN + "99AB", "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-A"),
    ]
    expect_422(events, [0])


def test_locked_missing_serial_returns_422():
    events = [
        event(scan(serial=None), "2026-09-01T00:00:00Z", "WH-A"),
        event(scan(), "2026-09-01T01:00:00Z", "WH-A"),
    ]
    expect_422(events, [0])


def test_locked_first_event_requires_chain_rebuild():
    # 锁定中间一条,迫使前面选能接上它的首条
    events = [
        event(scan(), "2026-09-01T00:00:00Z", "WH-Q"),            # 候选首条 ->Q
        event(scan(), "2026-09-01T00:30:00Z", "WH-A"),           # 候选首条 ->A
        event(scan(), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),  # 锁定
    ]
    body = trim(events, [2])
    # 为保留锁定事件 2,须选首条 1(A),剔除 0
    assert body["kept_indices"] == [1, 2]
    assert body["removed_indices"] == [0]
    assert body["items"][0]["holder"] == "WH-B"


# ---------- locked_indices 校验 ----------

def test_locked_indices_duplicate_422():
    events = [event(scan(), "2026-09-01T00:00:00Z", "WH-A")]
    expect_422(events, [0, 0])


def test_locked_indices_out_of_range_422():
    events = [event(scan(), "2026-09-01T00:00:00Z", "WH-A")]
    expect_422(events, [1])
    expect_422(events, [-1])


def test_locked_indices_empty_ok():
    events = [event(scan(), "2026-09-01T00:00:00Z", "WH-A")]
    body = trim(events, [])
    assert body["kept_indices"] == [0]


def test_locked_indices_bool_rejected():
    events = [event(scan(), "2026-09-01T00:00:00Z", "WH-A")]
    expect_422(events, [True])


def test_locked_indices_float_rejected():
    events = [event(scan(), "2026-09-01T00:00:00Z", "WH-A")]
    expect_422(events, [0.0])


# ---------- 请求体通用校验(复用现有事件模型) ----------

def test_validation_event_count():
    one = event(scan(), "2026-09-01T00:00:00Z", "WH-A")
    expect_422([])
    expect_422([one] * 301)
    resp = post([one] * 300)
    assert resp.status_code == 200, resp.text


def test_validation_event_fields_reused():
    expect_422([{"occurred_at": "2026-09-01T00:00:00Z", "from_location": None, "to_location": "A"}])
    expect_422([event(scan(), "2026-09-01T00:00:00", "WH-A")])
    expect_422([event(scan(), "2026-09-01T00:00:00Z", "")])
    expect_422([dict(event(scan(), "2026-09-01T00:00:00Z", "A"), extra=1)])


def test_extra_top_level_field_422():
    resp = client.post(
        "/v1/custody/trim",
        json={"events": [event(scan(), "2026-09-01T00:00:00Z", "WH-A")], "nope": 1},
    )
    assert resp.status_code == 422
