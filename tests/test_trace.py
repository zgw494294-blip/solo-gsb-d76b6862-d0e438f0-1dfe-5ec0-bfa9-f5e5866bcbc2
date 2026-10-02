"""POST /v1/recall/location-trace 的端到端测试。"""

from fastapi.testclient import TestClient

from app.gs1 import parse_scan
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


def scan(gtin=GTIN, serial="SN1", batch="LOT1", expiry="251231"):
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


def rule(rid, gtin=GTIN, **kw):
    return {"id": rid, "gtin": gtin, **kw}


def transfer(sc, at, to_loc, from_loc=None):
    return {
        "action": "transfer",
        "scan": sc,
        "occurred_at": at,
        "from_location": from_loc,
        "to_location": to_loc,
    }


def pack(parent, child, at="2026-09-01T00:00:00Z"):
    return {"action": "pack", "parent_scan": parent, "child_scan": child, "occurred_at": at}


def unpack(parent, child, at="2026-09-01T00:00:00Z"):
    return {"action": "unpack", "parent_scan": parent, "child_scan": child, "occurred_at": at}


def post(scans, rules, events, expected=200):
    resp = client.post(
        "/v1/recall/location-trace",
        json={"scans": scans, "rules": rules, "events": events},
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


PALLET = scan(GTIN, "PALLET")
CASE = scan(GTIN2, "CASE")
ITEM = scan(GTIN3, "ITEM")
ALL_AT_A = [
    transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A"),
    transfer(CASE, "2026-09-01T00:01:00Z", "WH-A"),
    transfer(ITEM, "2026-09-01T00:02:00Z", "WH-A"),
]


# ---------- 库存扫描 422 ----------


def test_inventory_invalid_scan_returns_422():
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": ["01" + GTIN + "99"],
            "rules": [rule("R1")],
            "events": [transfer(scan(), "2026-09-01T00:00:00Z", "WH-A")],
        },
    )
    assert resp.status_code == 422
    assert "库存扫描 0 非法" in resp.json()["detail"]


def test_inventory_missing_serial_returns_422():
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": ["01" + GTIN + "1725123110LOT1"],
            "rules": [rule("R1")],
            "events": [transfer(scan(), "2026-09-01T00:00:00Z", "WH-A")],
        },
    )
    assert resp.status_code == 422
    assert "缺少 AI 21" in resp.json()["detail"]


def test_inventory_duplicate_identity_returns_422():
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": [scan(GTIN, "A", batch="L1"), scan(GTIN, "A", batch="L2")],
            "rules": [rule("R1")],
            "events": [transfer(scan(GTIN, "A"), "2026-09-01T00:00:00Z", "WH-A")],
        },
    )
    assert resp.status_code == 422
    assert "重复" in resp.json()["detail"]


def test_validation_counts_fields_and_rules():
    good_event = transfer(scan(), "2026-09-01T00:00:00Z", "WH-A")

    def payload(scans, rules, events):
        return {"scans": scans, "rules": rules, "events": events}

    _expect_422(payload([], [rule("R1")], [good_event]))  # 空库存
    _expect_422(payload([scan()] * 101, [rule("R1")], [good_event]))
    _expect_422(payload([scan()], [], [good_event]))
    _expect_422(payload([scan()], [rule("R1")], []))
    _expect_422(payload([scan()], [rule("R1")], [good_event] * 201))
    _expect_422(payload([scan()], [rule("R1"), rule("R1")], [good_event]))  # 规则 ID 重复
    _expect_422(
        payload([scan()], [rule("R1", gtin="09506001123450")], [good_event])
    )  # GTIN 校验位
    # 边界:100 条库存、200 条事件合法
    scans100 = [scan(GTIN, f"SN{i}") for i in range(100)]
    body = post(
        scans100,
        [rule("R1")],
        [transfer(scans100[0], "2026-09-01T00:00:00Z", "WH-A")] * 200,
    )
    # 首次之后的交接 from_location 不为 null,全部报错,但请求合法
    assert len(body["errors"]) == 199


def test_validation_event_shapes():
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [{"action": "move", "occurred_at": "2026-09-01T00:00:00Z"}],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {
                    "action": "transfer",
                    "scan": scan(),
                    "occurred_at": "2026-09-01T00:00:00",  # 缺时区
                    "from_location": None,
                    "to_location": "A",
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {
                    "action": "transfer",
                    "scan": scan(),
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "from_location": None,
                    "to_location": "",  # 空接收方
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {  # transfer 缺 to_location
                    "action": "transfer",
                    "scan": scan(),
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "from_location": None,
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {  # pack 多余字段
                    "action": "pack",
                    "parent_scan": scan(),
                    "child_scan": scan(GTIN2, "C"),
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "to_location": "A",
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {  # 顶层多余字段
                    "scans": [scan()],
                    "extra": 1,
                }
            ],
        }
    )


def _expect_422(payload):
    resp = client.post("/v1/recall/location-trace", json=payload)
    assert resp.status_code == 422, resp.text


# ---------- 基本交接链与地点去重 ----------


def test_simple_transfer_chain_locations():
    events = [
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-C", from_loc="WH-B"),
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["errors"] == []
    assert body["items"] == [
        {"gtin": GTIN, "serial": "SN1", "locations": ["WH-A", "WH-B", "WH-C"], "rule_ids": ["R1"]}
    ]


def test_locations_deduped_by_first_arrival():
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-A", from_loc="WH-B"),
        transfer(scan(), "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_first_transfer_from_must_be_null():
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A", from_loc="WH-X"),  # 失败
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B"),  # 仍是首次,成功
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["items"][0]["locations"] == ["WH-B"]
    assert body["errors"][0]["index"] == 0
    assert "null" in body["errors"][0]["reason"]


def test_broken_chain_failed_event_has_no_effect():
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-X"),  # 链断裂
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),  # 以 A 为当前持有人,成功
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]
    assert [e["index"] for e in body["errors"]] == [1]


def test_utc_order_and_same_instant_input_order():
    events = [
        transfer(scan(), "2026-09-01T08:00:00+08:00", "WH-A"),  # UTC 00:00
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-B", from_loc="WH-A"),  # 同刻,依输入顺序
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["errors"] == []
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]
    # 反序后第二条的 from 不再成立
    body = post([scan()], [rule("R1")], list(reversed(events)))
    assert [e["index"] for e in body["errors"]] == [0]
    assert body["items"][0]["locations"] == ["WH-A"]


# ---------- 容器交接与后代传播 ----------


def test_container_transfer_moves_all_descendants():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert body["errors"] == []
    (item,) = body["items"]
    assert (item["gtin"], item["serial"]) == (GTIN3, "ITEM")
    # ITEM 自身先到 WH-A,随容器交接再到 WH-B
    assert item["locations"] == ["WH-A", "WH-B"]


def test_descendant_locations_dedup_across_container_transfers():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(PALLET, "2026-09-01T04:00:00Z", "WH-A", from_loc="WH-B"),  # 回到 WH-A,不重复
        transfer(PALLET, "2026-09-01T05:00:00Z", "WH-C", from_loc="WH-A"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert body["items"][0]["locations"] == ["WH-A", "WH-B", "WH-C"]


def test_transfer_with_parent_rejected():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        transfer(CASE, "2026-09-01T02:00:00Z", "WH-X", from_loc="WH-A"),  # CASE 已有父件
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [4]
    assert "仅无父件实物可直接交接" in body["errors"][0]["reason"]
    # 失败不改变持有人
    assert body["items"][0]["locations"] == ["WH-A"]


def test_unpack_keeps_current_holder_and_no_duplicate_location():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
        unpack(CASE, ITEM, "2026-09-01T04:00:00Z"),  # 取出:保留 WH-B,不新增地点
        transfer(ITEM, "2026-09-01T05:00:00Z", "WH-C", from_loc="WH-B"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert body["errors"] == []
    assert body["items"][0]["locations"] == ["WH-A", "WH-B", "WH-C"]


def test_unpack_only_removes_direct_edge():
    # PALLET->CASE->ITEM:只能取 CASE 出 PALLET,不能跨级取 ITEM
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        unpack(PALLET, ITEM, "2026-09-01T03:00:00Z"),  # 跨级,失败
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert [e["index"] for e in body["errors"]] == [5]
    # ITEM 仍在 CASE 下;容器交接仍带着 ITEM
    events2 = events + [transfer(PALLET, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A")]
    body2 = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events2)
    assert body2["errors"] and body2["items"][0]["locations"] == ["WH-A", "WH-B"]


# ---------- 装入约束 ----------


def test_pack_requires_same_holder():
    item_b = transfer(ITEM, "2026-09-01T00:00:00Z", "WH-B")
    pallet_a = transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A")
    events = [item_b, pallet_a, pack(PALLET, ITEM, "2026-09-01T01:00:00Z")]
    body = post([PALLET, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert [e["index"] for e in body["errors"]] == [2]
    assert "持有人须相同" in body["errors"][0]["reason"]


def test_pack_requires_existing_holder():
    # 双方都在库存中但从未交接,持有人缺失
    events = [pack(PALLET, CASE, "2026-09-01T01:00:00Z")]
    body = post([PALLET, CASE], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [0]
    assert "尚无成功交接" in body["errors"][0]["reason"]


def test_pack_child_already_parented_rejected():
    pallet2 = scan(GTIN4, "PALLET2")
    events = [
        transfer(pallet2, "2026-09-01T00:00:00Z", "WH-A"),
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(pallet2, CASE, "2026-09-01T02:00:00Z"),  # CASE 已有父件
    ]
    body = post([PALLET, pallet2, CASE, ITEM], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [5]
    assert "已有直接父件" in body["errors"][0]["reason"]


def test_pack_cycle_rejected():
    solo = scan(GTIN4, "SOLO")
    events = [
        transfer(solo, "2026-09-01T00:00:00Z", "WH-A"),
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        pack(ITEM, PALLET, "2026-09-01T03:00:00Z"),  # PALLET 是 ITEM 的祖先,成环
        pack(solo, solo, "2026-09-01T03:30:00Z"),  # 自装
    ]
    body = post([PALLET, CASE, ITEM, solo], [rule("R1", gtin=GTIN)], events)
    assert [e["index"] for e in body["errors"]] == [6, 7]
    assert "循环包装" in body["errors"][0]["reason"]
    assert "同一实物" in body["errors"][1]["reason"]
    # 失败装入不改变关系:PALLET 仍可整树交接
    events2 = events + [transfer(PALLET, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A")]
    body2 = post([PALLET, CASE, ITEM, solo], [rule("R1", gtin=GTIN3)], events2)
    assert body2["items"][0]["locations"] == ["WH-A", "WH-B"]


# ---------- 事件扫描校验(按原下标报错且不生效) ----------


def test_event_scan_invalid_and_missing_serial():
    events = [
        {"action": "transfer", "scan": "01" + GTIN + "99",
         "occurred_at": "2026-09-01T00:00:00Z", "from_location": None, "to_location": "WH-A"},
        {"action": "transfer", "scan": "01" + GTIN + "17251231",
         "occurred_at": "2026-09-01T01:00:00Z", "from_location": None, "to_location": "WH-B"},
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-C"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [0, 1]
    assert "扫描非法" in body["errors"][0]["reason"]
    assert "缺少 AI 21" in body["errors"][1]["reason"]
    assert body["items"][0]["locations"] == ["WH-C"]


def test_event_party_not_in_inventory():
    ghost = scan(GTIN4, "GHOST")
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(ghost, "2026-09-01T01:00:00Z", "WH-A"),
        pack(scan(), ghost, "2026-09-01T02:00:00Z"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [1, 2]
    assert all("不在库存扫描中" in e["reason"] for e in body["errors"])


def test_event_batch_expiry_must_match_inventory_including_absence():
    base = scan(batch="LOT1", expiry="251231")
    events = [
        transfer(base, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(batch="LOT2"), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(scan(expiry="260101"), "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(scan(batch=None), "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),  # 缺失冲突
        transfer(base, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A"),  # 完全一致,成功
    ]
    body = post([base], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [1, 2, 3]
    assert "批号" in body["errors"][0]["reason"]
    assert "有效期" in body["errors"][1]["reason"]
    assert "批号" in body["errors"][2]["reason"]
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_inventory_without_batch_or_expiry_event_must_also_omit():
    bare = scan(batch=None, expiry=None)
    events = [
        transfer(bare, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(batch="LOT1", expiry=None),
                 "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),  # 多了批号
        transfer(bare, "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([bare], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [1]
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_failed_event_does_not_change_holder_or_tree():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(PALLET, CASE, "2026-09-01T02:00:00Z"),  # 重复装入失败,关系不变
        unpack(PALLET, CASE, "2026-09-01T03:00:00Z"),  # 正常取出成功
        transfer(PALLET, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A"),
        # CASE 已取出,不随 PALLET 移动;其当前持有人仍为 WH-A
        transfer(CASE, "2026-09-01T05:00:00Z", "WH-C", from_loc="WH-A"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [4]
    assert body["items"][0]["locations"] == ["WH-A", "WH-C"]


# ---------- 召回匹配与输出 ----------


def test_only_recalled_items_returned_sorted():
    a = scan(GTIN, "A")
    b = scan(GTIN, "B")
    c = scan(GTIN2, "C")
    events = [
        transfer(c, "2026-09-01T00:00:00Z", "L1"),
        transfer(b, "2026-09-01T00:00:00Z", "L2"),
        transfer(a, "2026-09-01T00:00:00Z", "L3"),
    ]
    body = post([a, b, c], [rule("R1"), rule("R2", gtin=GTIN4)], events)
    # 仅命中 GTIN 的 A、B;按 GTIN、序列号排序;未命中的 R2 不出现
    assert body["items"] == [
        {"gtin": GTIN, "serial": "A", "locations": ["L3"], "rule_ids": ["R1"]},
        {"gtin": GTIN, "serial": "B", "locations": ["L2"], "rule_ids": ["R1"]},
    ]
    assert body["errors"] == []


def test_rule_ids_in_rule_input_order_and_item_without_events():
    item = scan(GTIN3, "NEVER-MOVED")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3), rule("R2", gtin=GTIN3), rule("R3", gtin=GTIN4)],
        [transfer(scan(GTIN4, "OTHER"), "2026-09-01T00:00:00Z", "WH-A")],
    )
    assert body["items"] == [
        {"gtin": GTIN3, "serial": "NEVER-MOVED", "locations": [], "rule_ids": ["R1", "R2"]}
    ]


def test_rule_fields_reused_for_matching():
    s = scan(GTIN, "A", batch="LOT1", expiry="251231")
    body = post(
        [s],
        [
            rule("R-batch", batch="LOT2"),
            rule("R-date", date_from="2026-01-01"),
            rule("R-hit", batch="LOT1", date_to="2025-12-31"),
        ],
        [transfer(s, "2026-09-01T00:00:00Z", "WH-A")],
    )
    assert body["items"][0]["rule_ids"] == ["R-hit"]


def test_event_errors_sorted_by_original_index():
    events = [
        transfer(scan(GTIN4, "X"), "2026-09-01T02:00:00Z", "WH-A"),  # 0 不在库存
        {"action": "transfer", "scan": "01" + GTIN + "99",
         "occurred_at": "2026-09-01T01:00:00Z", "from_location": None, "to_location": "A"},  # 1 非法
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A", from_loc="WH-Z"),  # 2 首次非 null
    ]
    body = post([scan()], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [0, 1, 2]


# ---------- 同地共处时段(contact_until)----------

RECALLED = scan(GTIN, "R1")  # 命中召回
UNRECALLED = scan(GTIN2, "U1")  # 未命中召回
UNTIL = "2026-09-01T06:00:00Z"


def post_contacts(scans, rules, events, until=UNTIL, expected=200):
    resp = client.post(
        "/v1/recall/location-trace",
        json={"scans": scans, "rules": rules, "events": events, "contact_until": until},
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def contact(item_key, recalled_key, location, start, end):
    """构造期望的 contacts 条目(双方均为 (GTIN, 序列号))。"""
    return {
        "item": {"gtin": item_key[0], "serial": item_key[1]},
        "recalled_item": {"gtin": recalled_key[0], "serial": recalled_key[1]},
        "location": location,
        "start": start,
        "end": end,
    }


def key_of(sc):
    """从扫描串取 (GTIN, 序列号),便于断言。"""
    parsed = parse_scan(sc)
    return parsed.gtin, parsed.serial


def test_contact_until_omitted_response_has_no_contacts_key():
    events = [transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A")]
    body = post([RECALLED, UNRECALLED], [rule("R1")], events)
    assert "contacts" not in body  # 省略时响应与原有一致


def test_contact_until_must_be_timezone_aware():
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": [RECALLED],
            "rules": [rule("R1")],
            "events": [transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A")],
            "contact_until": "2026-09-01T06:00:00",  # 缺时区
        },
    )
    assert resp.status_code == 422


def test_contact_until_must_not_precede_any_event():
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(RECALLED, "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    # 早于最晚事件(即使晚于最早事件)也须 422
    post_contacts([RECALLED], [rule("R1")], events, until="2026-09-01T01:00:00Z", expected=422)
    post_contacts([RECALLED], [rule("R1")], events, until="2026-08-31T23:00:00Z", expected=422)
    # 等于最晚事件时刻合法
    body = post_contacts([RECALLED], [rule("R1")], events, until="2026-09-01T02:00:00Z")
    assert body["contacts"] == []


def test_contact_until_accepts_non_utc_timezone():
    events = [transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
              transfer(UNRECALLED, "2026-09-01T00:10:00Z", "WH-A")]
    body = post_contacts(
        [RECALLED, UNRECALLED], [rule("R1")], events, until="2026-09-01T14:00:00+08:00"
    )
    assert body["contacts"] == [
        contact(key_of(UNRECALLED), key_of(RECALLED), "WH-A",
                "2026-09-01T00:10:00Z", "2026-09-01T06:00:00Z")
    ]


def test_contacts_basic_colocation_and_window_end():
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(UNRECALLED, "2026-09-01T00:30:00Z", "WH-A"),
        transfer(RECALLED, "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(UNRECALLED, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events, until="2026-09-01T05:00:00Z")
    assert body["errors"] == []
    assert body["contacts"] == [
        contact(key_of(UNRECALLED), key_of(RECALLED), "WH-A",
                "2026-09-01T00:30:00Z", "2026-09-01T02:00:00Z"),
        contact(key_of(UNRECALLED), key_of(RECALLED), "WH-B",
                "2026-09-01T03:00:00Z", "2026-09-01T05:00:00Z"),
    ]
    # 原结果保留:命中实物的地点列表不变
    assert body["items"] == [
        {"gtin": GTIN, "serial": "R1", "locations": ["WH-A", "WH-B"], "rule_ids": ["R1"]}
    ]


def test_contacts_same_instant_events_processed_before_interval():
    # 同一时刻两条事件全部处理完后才计算接下来时段:共处从该时刻开始
    events = [
        transfer(UNRECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
    ]
    body = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events,
                         until="2026-09-01T01:00:00Z")
    assert body["contacts"] == [
        contact(key_of(UNRECALLED), key_of(RECALLED), "WH-A",
                "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z")
    ]
    # 同刻离开 + 到达:以整组处理完后的状态计,不产生共处
    events2 = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(RECALLED, "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(UNRECALLED, "2026-09-01T01:00:00Z", "WH-A"),
    ]
    body2 = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events2,
                          until="2026-09-01T02:00:00Z")
    assert body2["contacts"] == []


def test_contacts_container_transfer_moves_descendants():
    # PALLET、CASE 未命中,ITEM 命中;容器交接带动后代,共处地点随之变化
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post_contacts([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events,
                         until="2026-09-01T04:00:00Z")
    pallet, case, item = key_of(PALLET), key_of(CASE), key_of(ITEM)
    assert body["contacts"] == [
        contact(pallet, item, "WH-A", "2026-09-01T00:02:00Z", "2026-09-01T03:00:00Z"),
        contact(pallet, item, "WH-B", "2026-09-01T03:00:00Z", "2026-09-01T04:00:00Z"),
        contact(case, item, "WH-A", "2026-09-01T00:02:00Z", "2026-09-01T03:00:00Z"),
        contact(case, item, "WH-B", "2026-09-01T03:00:00Z", "2026-09-01T04:00:00Z"),
    ]


def test_contacts_unpack_keeps_holder_and_merges_adjacent():
    # 取出保留持有人:unpack 不改变共处;相接时段(跨 pack/unpack/无关事件)合并
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        unpack(CASE, ITEM, "2026-09-01T03:00:00Z"),  # 取出后 ITEM 仍持有 WH-A
    ]
    body = post_contacts([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events,
                         until="2026-09-01T04:00:00Z")
    pallet, case, item = key_of(PALLET), key_of(CASE), key_of(ITEM)
    assert body["contacts"] == [
        contact(pallet, item, "WH-A", "2026-09-01T00:02:00Z", "2026-09-01T04:00:00Z"),
        contact(case, item, "WH-A", "2026-09-01T00:02:00Z", "2026-09-01T04:00:00Z"),
    ]


def test_contacts_unknown_holder_not_counted():
    # 未命中实物从未成功交接(持有人未知),不计共处
    events = [transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A")]
    body = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events)
    assert body["contacts"] == []


def test_contacts_only_unmatched_vs_recalled_no_transitive():
    other_recalled = scan(GTIN, "R2")  # 同 GTIN,同样命中
    bystander = scan(GTIN3, "U2")  # 未命中,只与未命中实物共处
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(other_recalled, "2026-09-01T00:05:00Z", "WH-A"),  # 命中 × 命中不列
        transfer(UNRECALLED, "2026-09-01T00:10:00Z", "WH-A"),
        transfer(RECALLED, "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(other_recalled, "2026-09-01T01:05:00Z", "WH-B", from_loc="WH-A"),
        transfer(bystander, "2026-09-01T02:00:00Z", "WH-A"),  # 仅与未命中的 U1 共处
    ]
    body = post_contacts(
        [RECALLED, other_recalled, UNRECALLED, bystander], [rule("R1")], events,
        until="2026-09-01T03:00:00Z",
    )
    u1, r1, r2 = key_of(UNRECALLED), key_of(RECALLED), key_of(other_recalled)
    # 不作传递扩散:bystander 不因与 U1 共处而关联到命中实物
    assert body["contacts"] == [
        contact(u1, r1, "WH-A", "2026-09-01T00:10:00Z", "2026-09-01T01:00:00Z"),
        contact(u1, r2, "WH-A", "2026-09-01T00:10:00Z", "2026-09-01T01:05:00Z"),
    ]


def test_contacts_merge_across_unrelated_events_and_split_on_gap():
    extra = scan(GTIN4, "X1")  # 未命中,独自在 WH-Z
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(UNRECALLED, "2026-09-01T00:10:00Z", "WH-A"),
        transfer(extra, "2026-09-01T00:20:00Z", "WH-Z"),  # 无关事件不打断共处
        transfer(UNRECALLED, "2026-09-01T00:30:00Z", "WH-B", from_loc="WH-A"),
        transfer(UNRECALLED, "2026-09-01T00:40:00Z", "WH-A", from_loc="WH-B"),
    ]
    body = post_contacts([RECALLED, UNRECALLED, extra], [rule("R1")], events,
                         until="2026-09-01T01:00:00Z")
    u1, r1 = key_of(UNRECALLED), key_of(RECALLED)
    # 离开再回来:两段不相接,分别列出;中间跨无关事件的时段合并
    assert body["contacts"] == [
        contact(u1, r1, "WH-A", "2026-09-01T00:10:00Z", "2026-09-01T00:30:00Z"),
        contact(u1, r1, "WH-A", "2026-09-01T00:40:00Z", "2026-09-01T01:00:00Z"),
    ]


def test_contacts_positive_duration_only():
    # contact_until 等于最后事件时刻:最后一段零时长,不产生共处
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(UNRECALLED, "2026-09-01T01:00:00Z", "WH-A"),
    ]
    body = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events,
                         until="2026-09-01T01:00:00Z")
    assert body["contacts"] == []


def test_contacts_failed_events_not_effective():
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(UNRECALLED, "2026-09-01T00:10:00Z", "WH-A", from_loc="WH-X"),  # 失败
        transfer(UNRECALLED, "2026-09-01T00:20:00Z", "WH-A"),  # 首次成功交接
    ]
    body = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events,
                         until="2026-09-01T01:00:00Z")
    assert [e["index"] for e in body["errors"]] == [1]
    assert body["contacts"] == [
        contact(key_of(UNRECALLED), key_of(RECALLED), "WH-A",
                "2026-09-01T00:20:00Z", "2026-09-01T01:00:00Z")
    ]


def test_contacts_sorted_by_item_recalled_start_location():
    ub = scan(GTIN2, "B")  # 未命中,GTIN2 < GTIN3
    ua = scan(GTIN3, "A")  # 未命中
    r1 = scan(GTIN, "R1")
    r2 = scan(GTIN, "R2")  # 同 GTIN 也命中,序列号 R1 < R2
    events = [
        transfer(r2, "2026-09-01T00:00:00Z", "L2"),
        transfer(ua, "2026-09-01T00:05:00Z", "L2"),
        transfer(r1, "2026-09-01T00:10:00Z", "L1"),
        transfer(ub, "2026-09-01T00:15:00Z", "L1"),
        transfer(ua, "2026-09-01T00:20:00Z", "L1", from_loc="L2"),
    ]
    body = post_contacts([r1, r2, ua, ub], [rule("R1")], events,
                         until="2026-09-01T01:00:00Z")
    assert body["contacts"] == [
        contact(key_of(ub), key_of(r1), "L1", "2026-09-01T00:15:00Z", "2026-09-01T01:00:00Z"),
        contact(key_of(ua), key_of(r1), "L1", "2026-09-01T00:20:00Z", "2026-09-01T01:00:00Z"),
        contact(key_of(ua), key_of(r2), "L2", "2026-09-01T00:05:00Z", "2026-09-01T00:20:00Z"),
    ]


def test_contacts_empty_array_when_no_colocation():
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(UNRECALLED, "2026-09-01T00:10:00Z", "WH-B"),
    ]
    body = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events)
    assert body["contacts"] == []  # 提供 contact_until 但无共处:空数组而非缺省


def test_contacts_items_and_errors_unchanged_with_contact_until():
    events = [
        transfer(RECALLED, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(UNRECALLED, "2026-09-01T00:10:00Z", "WH-A", from_loc="WH-X"),  # 失败
        transfer(UNRECALLED, "2026-09-01T00:20:00Z", "WH-A"),
        transfer(RECALLED, "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    plain = post([RECALLED, UNRECALLED], [rule("R1")], events)
    full = post_contacts([RECALLED, UNRECALLED], [rule("R1")], events)
    assert full["items"] == plain["items"]
    assert full["errors"] == plain["errors"]
    assert "contacts" in full and "contacts" not in plain


# ---------- 旧接口回归 ----------
def test_existing_endpoints_still_available():
    assert client.get("/healthz").json() == {"status": "ok"}
    custody_event = {
        "scan": scan(),
        "occurred_at": "2026-09-01T00:00:00Z",
        "from_location": None,
        "to_location": "WH-A",
    }
    resp = client.post("/v1/custody/audit", json={"events": [custody_event]})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["holder"] == "WH-A"
    resp = client.post(
        "/v1/packing/audit",
        json={"operations": [{"action": "pack",
                              "parent_scan": scan(GTIN, "P"),
                              "child_scan": scan(GTIN2, "C")}]},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2
