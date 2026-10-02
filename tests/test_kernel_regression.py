"""统一重放内核(app.replay)的小规模嵌套与失败后续操作回归。

覆盖三种接口共用内核后的关键语义:

- 独立装拆不要求库存 / 持有人,失败操作不把实物加入成功涉及集合,失败后可继续;
- 独立交接的批号 / 有效期基准取首次成功事件,失败事件不留半次更新;
- 地点追溯以库存为基准、装入须双方已有相同持有人、根件交接带动全部后代、
  取出保留当时持有人;
- 召回隔离计划继续复用同一内核入口(packing.replay_packing)。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FNC1 = "\x1d"


def make_gtin(body13: str) -> str:
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")
GTIN3 = make_gtin("0950600112347")
GTIN4 = make_gtin("0950600112348")
GTIN5 = make_gtin("0950600112349")


def scan(gtin=GTIN, serial="SN1", batch=None, expiry=None):
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


def pack(p, c):
    return {"action": "pack", "parent_scan": p, "child_scan": c}


def unpack(p, c):
    return {"action": "unpack", "parent_scan": p, "child_scan": c}


# ---------- 独立装拆:小规模嵌套 + 失败后续 ----------


def test_packing_three_level_nesting_with_failed_ops_in_between():
    root = scan(GTIN, "R")
    mid = scan(GTIN2, "M")
    leaf = scan(GTIN3, "L")
    stranger = scan(GTIN4, "X")
    body = client.post(
        "/v1/packing/audit",
        json={
            "operations": [
                pack(root, mid),
                pack(stranger, mid),  # 失败:mid 已有父件;X 不得进入涉及集合
                pack(mid, leaf),  # 继续:三层嵌套成立
                unpack(stranger, leaf),  # 失败:X 从未在成功操作中出现
                unpack(mid, leaf),  # 成功取出
                pack(root, leaf),  # leaf 取出后可直接装入根件
            ]
        },
    ).json()
    assert [e["index"] for e in body["errors"]] == [1, 3]
    im = {(i["gtin"], i["serial"]): i for i in body["items"]}
    # X 仅出现在失败操作中,不得列出
    assert (GTIN4, "X") not in im
    assert im[(GTIN2, "M")]["parent"] == {"gtin": GTIN, "serial": "R"}
    assert im[(GTIN3, "L")]["parent"] == {"gtin": GTIN, "serial": "R"}
    assert im[(GTIN3, "L")]["root"] == {"gtin": GTIN, "serial": "R"}
    assert im[(GTIN, "R")]["children"] == [
        {"gtin": GTIN2, "serial": "M"},
        {"gtin": GTIN3, "serial": "L"},
    ]


def test_packing_failed_cycle_leaves_no_edge_and_later_unpack_still_works():
    root = scan(GTIN, "R")
    mid = scan(GTIN2, "M")
    leaf = scan(GTIN3, "L")
    body = client.post(
        "/v1/packing/audit",
        json={
            "operations": [
                pack(root, mid),
                pack(mid, leaf),
                pack(leaf, root),  # 成环失败,不留边
                unpack(root, mid),  # 失败后关系不变:root 仍是 mid 的直接父件?此操作应成功
            ]
        },
    ).json()
    assert [e["index"] for e in body["errors"]] == [2]
    im = {(i["gtin"], i["serial"]): i for i in body["items"]}
    # root->mid 已取出
    assert im[(GTIN2, "M")]["parent"] is None
    assert im[(GTIN3, "L")]["parent"] == {"gtin": GTIN2, "serial": "M"}


def test_packing_failed_pack_does_not_register_parent_as_known():
    c = scan(GTIN2, "C")
    ghost = scan(GTIN4, "G")
    body = client.post(
        "/v1/packing/audit",
        json={
            "operations": [
                pack(ghost, c),  # 成功:c 与 ghost 登记
                unpack(scan(GTIN5, "NEVER"), c),  # 失败:父件未知
            ]
        },
    ).json()
    assert [e["index"] for e in body["errors"]] == [1]
    im = {(i["gtin"], i["serial"]): i for i in body["items"]}
    assert (GTIN5, "NEVER") not in im


# ---------- 独立交接:首次成功基准 + 失败不留半次更新 ----------


def transfer_event(sc, at, to_loc, from_loc=None):
    return {
        "scan": sc,
        "occurred_at": at,
        "from_location": from_loc,
        "to_location": to_loc,
    }


def test_custody_baseline_from_first_success_after_failures():
    # 下标 0 首次事件 from 非 null 失败;下标 2 批号冲突失败。
    # 基准必须取自首次**成功**事件(下标 1,LOT-B 无有效期)。
    base = scan(batch="LOT-B", expiry=None)
    events = [
        transfer_event(base, "2026-09-01T00:00:00Z", "WH-A", from_loc="WH-X"),
        transfer_event(base, "2026-09-01T01:00:00Z", "WH-A"),
        transfer_event(scan(batch="LOT-B", expiry="260101"),
                       "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
        transfer_event(base, "2026-09-01T03:00:00Z", "WH-C", from_loc="WH-A"),
    ]
    body = client.post("/v1/custody/audit", json={"events": events}).json()
    assert [e["index"] for e in body["errors"]] == [0, 2]
    assert body["items"] == [
        {"gtin": GTIN, "serial": "SN1", "events": [1, 3], "holder": "WH-C"}
    ]


def test_custody_failed_chain_event_does_not_advance_holder():
    s = scan(batch=None, expiry=None)
    events = [
        transfer_event(s, "2026-09-01T00:00:00Z", "WH-A"),
        transfer_event(s, "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-Z"),
        transfer_event(s, "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
        transfer_event(s, "2026-09-01T03:00:00Z", "WH-A", from_loc="WH-B"),
    ]
    body = client.post("/v1/custody/audit", json={"events": events}).json()
    assert [e["index"] for e in body["errors"]] == [1]
    assert body["items"][0]["events"] == [0, 2, 3]
    assert body["items"][0]["holder"] == "WH-A"


# ---------- 地点追溯:嵌套容器、根件带动后代、取出保留持有人 ----------


def trace_transfer(sc, at, to_loc, from_loc=None):
    return {
        "action": "transfer",
        "scan": sc,
        "occurred_at": at,
        "from_location": from_loc,
        "to_location": to_loc,
    }


def trace_pack(p, c, at):
    return {"action": "pack", "parent_scan": p, "child_scan": c, "occurred_at": at}


def trace_unpack(p, c, at):
    return {"action": "unpack", "parent_scan": p, "child_scan": c, "occurred_at": at}


def test_trace_nested_container_and_unpack_keeps_holder_after_failures():
    pallet = scan(GTIN, "PALLET")
    case = scan(GTIN2, "CASE")
    item = scan(GTIN3, "ITEM")
    other = scan(GTIN4, "OTHER")
    events = [
        trace_transfer(pallet, "2026-09-01T00:00:00Z", "WH-A"),
        trace_transfer(case, "2026-09-01T00:01:00Z", "WH-A"),
        trace_transfer(item, "2026-09-01T00:02:00Z", "WH-A"),
        trace_transfer(other, "2026-09-01T00:03:00Z", "WH-A"),  # 不在库存,失败
        trace_pack(pallet, case, "2026-09-01T01:00:00Z"),
        trace_pack(case, other, "2026-09-01T01:30:00Z"),  # 引用库存外实物,失败不留边
        trace_pack(case, item, "2026-09-01T02:00:00Z"),
        trace_transfer(case, "2026-09-01T02:30:00Z", "WH-X", from_loc="WH-A"),  # 有父件,失败
        trace_transfer(pallet, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
        trace_unpack(pallet, item, "2026-09-01T03:30:00Z"),  # 跨级取出失败
        trace_unpack(case, item, "2026-09-01T04:00:00Z"),  # 取出,保留 WH-B
        trace_transfer(item, "2026-09-01T05:00:00Z", "WH-C", from_loc="WH-B"),
    ]
    body = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": [pallet, case, item],
            "rules": [{"id": "R1", "gtin": GTIN3}],
            "events": events,
        },
    ).json()
    assert [e["index"] for e in body["errors"]] == [3, 5, 7, 9]
    assert body["items"] == [
        {
            "gtin": GTIN3,
            "serial": "ITEM",
            "locations": ["WH-A", "WH-B", "WH-C"],
            "rule_ids": ["R1"],
        }
    ]


def test_trace_pack_requires_same_holder_failure_is_atomic():
    pallet = scan(GTIN, "P")
    a = scan(GTIN2, "A")
    b = scan(GTIN3, "B")
    events = [
        trace_transfer(pallet, "2026-09-01T00:00:00Z", "WH-A"),
        trace_transfer(a, "2026-09-01T00:01:00Z", "WH-A"),
        trace_transfer(b, "2026-09-01T00:02:00Z", "WH-B"),
        trace_pack(pallet, b, "2026-09-01T01:00:00Z"),  # 持有人不同,失败
        # 失败不留边:b 仍是无父件,可自行交接;pallet 交接不带动 b
        trace_transfer(pallet, "2026-09-01T02:00:00Z", "WH-C", from_loc="WH-A"),
        trace_transfer(b, "2026-09-01T03:00:00Z", "WH-A", from_loc="WH-B"),
        trace_pack(pallet, a, "2026-09-01T04:00:00Z"),  # 此时 pallet 在 C、a 在 A → 仍失败
    ]
    body = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": [pallet, a, b],
            "rules": [{"id": "R1", "gtin": GTIN3}],
            "events": events,
        },
    ).json()
    assert [e["index"] for e in body["errors"]] == [3, 6]
    # b 不随 pallet 移动到 WH-C;其自身轨迹为 B -> A
    assert body["items"][0]["locations"] == ["WH-B", "WH-A"]


# ---------- 召回隔离计划继续走同一内核 ----------


def test_isolation_independent_packing_has_no_holder_constraint():
    # 召回隔离的装拆重放走内核的独立装拆模式:没有库存 / 持有人约束,
    # case2 装入 case1 成功后,整条父链都归并到已封签的 pallet。
    pallet = scan(GTIN, "PALLET")
    case1 = scan(GTIN2, "CASE1")
    case2 = scan(GTIN3, "CASE2")
    body = client.post(
        "/v1/recall/isolation-plan",
        json={
            "scans": [case1, case2],
            "rules": [
                {"id": "R1", "gtin": GTIN2},
                {"id": "R2", "gtin": GTIN3},
            ],
            "operations": [
                pack(pallet, case1),
                pack(case1, case2),
            ],
            "sealed_scans": [pallet],
        },
    ).json()
    assert body["operation_errors"] == []
    assert body["units"] == [
        {"gtin": GTIN, "serial": "PALLET", "scan_indices": [0, 1], "rule_ids": ["R1", "R2"]}
    ]


def test_isolation_failed_repack_keeps_original_unit_mapping():
    pallet = scan(GTIN, "PALLET")
    other = scan(GTIN4, "OTHER")
    case = scan(GTIN2, "CASE")
    body = client.post(
        "/v1/recall/isolation-plan",
        json={
            "scans": [case],
            "rules": [{"id": "R1", "gtin": GTIN2}],
            "operations": [
                pack(pallet, case),
                pack(other, case),  # 重复装入失败:不改父边,other 不进涉及集合
            ],
            "sealed_scans": [pallet],
        },
    ).json()
    assert [e["index"] for e in body["operation_errors"]] == [1]
    assert body["units"] == [
        {"gtin": GTIN, "serial": "PALLET", "scan_indices": [0], "rule_ids": ["R1"]}
    ]
