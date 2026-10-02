"""POST /v1/packing/audit 的端到端测试。"""

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


def op(action, parent, child):
    return {"action": action, "parent_scan": parent, "child_scan": child}


def pack(parent, child):
    return op("pack", parent, child)


def unpack(parent, child):
    return op("unpack", parent, child)


def post(operations):
    resp = client.post("/v1/packing/audit", json={"operations": operations})
    assert resp.status_code == 200, resp.text
    return resp.json()


def item_map(body):
    return {(i["gtin"], i["serial"]): i for i in body["items"]}


# ---------- 正常路径 ----------

def test_single_pack_returns_both_items_with_relations():
    p = scan(GTIN, "PALLET1")
    c = scan(GTIN2, "BOX1")
    body = post([pack(p, c)])
    assert body["errors"] == []
    im = item_map(body)
    assert [(i["gtin"], i["serial"]) for i in body["items"]] == [
        (GTIN, "PALLET1"),
        (GTIN2, "BOX1"),
    ]
    parent_item = im[(GTIN, "PALLET1")]
    assert parent_item["parent"] is None
    assert parent_item["children"] == [{"gtin": GTIN2, "serial": "BOX1"}]
    assert parent_item["root"] == {"gtin": GTIN, "serial": "PALLET1"}  # 无父件时最外层为自身
    child_item = im[(GTIN2, "BOX1")]
    assert child_item["parent"] == {"gtin": GTIN, "serial": "PALLET1"}
    assert child_item["children"] == []
    assert child_item["root"] == {"gtin": GTIN, "serial": "PALLET1"}


def test_items_sorted_by_gtin_then_serial():
    body = post(
        [
            pack(scan(GTIN, "P"), scan(GTIN, "Z")),
            pack(scan(GTIN, "P"), scan(GTIN, "A")),
            pack(scan(GTIN, "P"), scan(GTIN2, "M")),
        ]
    )
    assert body["errors"] == []
    assert [(i["gtin"], i["serial"]) for i in body["items"]] == [
        (GTIN, "A"),
        (GTIN, "P"),
        (GTIN, "Z"),
        (GTIN2, "M"),
    ]
    # 直接子件同样按 GTIN、序列号排序
    assert item_map(body)[(GTIN, "P")]["children"] == [
        {"gtin": GTIN, "serial": "A"},
        {"gtin": GTIN, "serial": "Z"},
        {"gtin": GTIN2, "serial": "M"},
    ]


def test_nested_pack_outermost_container_walks_to_root():
    root = scan(GTIN, "R")
    mid = scan(GTIN2, "M")
    leaf = scan(GTIN3, "L")
    body = post([pack(root, mid), pack(mid, leaf)])
    assert body["errors"] == []
    im = item_map(body)
    assert im[(GTIN3, "L")]["root"] == {"gtin": GTIN, "serial": "R"}
    assert im[(GTIN3, "L")]["parent"] == {"gtin": GTIN2, "serial": "M"}
    assert im[(GTIN2, "M")]["root"] == {"gtin": GTIN, "serial": "R"}
    assert im[(GTIN, "R")]["root"] == {"gtin": GTIN, "serial": "R"}
    assert im[(GTIN, "R")]["children"] == [{"gtin": GTIN2, "serial": "M"}]


def test_symbol_id_prefix_and_fnc1_accepted():
    p = scan(GTIN, "P", batch="L1", prefix=True)
    c = scan(GTIN2, "C", batch="L2", expiry="251231", prefix=True)
    body = post([pack(p, c)])
    assert body["errors"] == []
    assert len(body["items"]) == 2


# ---------- 装入约束 ----------

def test_pack_child_already_has_parent_fails_without_change():
    p1 = scan(GTIN, "P1")
    p2 = scan(GTIN2, "P2")
    c = scan(GTIN3, "C")
    body = post([pack(p1, c), pack(p2, c)])
    assert [e["index"] for e in body["errors"]] == [1]
    assert "已有" in body["errors"][0]["reason"]
    im = item_map(body)
    assert im[(GTIN3, "C")]["parent"] == {"gtin": GTIN, "serial": "P1"}  # 未被改写
    # P2 仅出现在失败操作中,不应列出
    assert (GTIN2, "P2") not in im


def test_pack_self_fails():
    s = scan(GTIN, "S")
    body = post([pack(s, s)])
    assert [e["index"] for e in body["errors"]] == [0]
    assert "同一实物" in body["errors"][0]["reason"]
    assert body["items"] == []  # 操作失败,不涉及任何实物


def test_pack_ancestor_into_descendant_fails():
    root = scan(GTIN, "R")
    mid = scan(GTIN2, "M")
    leaf = scan(GTIN3, "L")
    # 先 R 装 M、M 装 L;再试图 L 装 R(把祖先装进后代)→ 成环
    body = post([pack(root, mid), pack(mid, leaf), pack(leaf, root)])
    assert [e["index"] for e in body["errors"]] == [2]
    assert "祖先" in body["errors"][0]["reason"]
    im = item_map(body)
    assert im[(GTIN, "R")]["parent"] is None
    assert im[(GTIN2, "M")]["parent"] == {"gtin": GTIN, "serial": "R"}
    assert im[(GTIN3, "L")]["parent"] == {"gtin": GTIN2, "serial": "M"}


def test_pack_grandparent_into_grandchild_fails():
    root = scan(GTIN, "R")
    mid = scan(GTIN2, "M")
    leaf = scan(GTIN3, "L")
    # 试图把祖父 R 装进孙子 M:R 是 M 的祖先
    body = post([pack(root, mid), pack(mid, leaf), pack(leaf, mid)])
    assert [e["index"] for e in body["errors"]] == [2]


def test_pack_parent_not_known_is_registered_as_orphan():
    # 子件先作为父件出现过(无父件),再把另一父件装入它是允许的——此处验证父件首次出现即登记
    p = scan(GTIN, "P")
    c = scan(GTIN2, "C")
    body = post([pack(p, c)])
    im = item_map(body)
    assert im[(GTIN, "P")]["parent"] is None


# ---------- 取出约束 ----------

def test_unpack_removes_direct_edge():
    p = scan(GTIN, "P")
    c = scan(GTIN2, "C")
    body = post([pack(p, c), unpack(p, c)])
    assert body["errors"] == []
    im = item_map(body)
    # 取出后两者仍列出(成功操作涉及),但无父子关系
    assert im[(GTIN, "P")]["children"] == []
    assert im[(GTIN, "P")]["root"] == {"gtin": GTIN, "serial": "P"}
    assert im[(GTIN2, "C")]["parent"] is None
    assert im[(GTIN2, "C")]["root"] == {"gtin": GTIN2, "serial": "C"}  # 无父件时最外层为自身


def test_unpack_wrong_parent_fails():
    p = scan(GTIN, "P")
    other = scan(GTIN2, "O")
    c = scan(GTIN3, "C")
    d = scan(GTIN4, "D")
    body = post([pack(p, c), pack(other, d), unpack(other, c)])
    assert [e["index"] for e in body["errors"]] == [2]
    assert "直接父件" in body["errors"][0]["reason"]
    im = item_map(body)
    assert im[(GTIN3, "C")]["parent"] == {"gtin": GTIN, "serial": "P"}


def test_unpack_grandparent_fails_must_be_direct_parent():
    root = scan(GTIN, "R")
    mid = scan(GTIN2, "M")
    leaf = scan(GTIN3, "L")
    body = post([pack(root, mid), pack(mid, leaf), unpack(root, leaf)])
    assert [e["index"] for e in body["errors"]] == [2]
    im = item_map(body)
    assert im[(GTIN3, "L")]["parent"] == {"gtin": GTIN2, "serial": "M"}


def test_unpack_unknown_child_fails():
    p = scan(GTIN, "P")
    c = scan(GTIN2, "C")
    body = post([unpack(p, c)])
    assert [e["index"] for e in body["errors"]] == [0]
    assert body["items"] == []


def test_repack_after_unpack_allowed():
    p1 = scan(GTIN, "P1")
    p2 = scan(GTIN2, "P2")
    c = scan(GTIN3, "C")
    body = post([pack(p1, c), unpack(p1, c), pack(p2, c)])
    assert body["errors"] == []
    im = item_map(body)
    assert im[(GTIN3, "C")]["parent"] == {"gtin": GTIN2, "serial": "P2"}
    assert im[(GTIN3, "C")]["root"] == {"gtin": GTIN2, "serial": "P2"}
    assert im[(GTIN, "P1")]["children"] == []


# ---------- 扫描问题 ----------

def test_invalid_parent_scan_reported_by_index():
    c = scan(GTIN2, "C")
    body = post([pack("01" + GTIN + "99", c)])
    assert [e["index"] for e in body["errors"]] == [0]
    assert "父件扫描非法" in body["errors"][0]["reason"]
    assert body["items"] == []


def test_invalid_child_scan_reported_by_index():
    p = scan(GTIN, "P")
    body = post([pack(p, "01" + GTIN2 + "99")])
    assert [e["index"] for e in body["errors"]] == [0]
    assert "子件扫描非法" in body["errors"][0]["reason"]
    # 父件解析合法但操作整体失败 → 父件也不属于成功操作涉及实物
    assert body["items"] == []


def test_missing_serial_reported_for_either_side():
    p_no_serial = "01" + GTIN + "17251231"
    c = scan(GTIN2, "C")
    body = post([pack(p_no_serial, c), pack(c, p_no_serial)])
    assert [e["index"] for e in body["errors"]] == [0, 1]
    assert "父件扫描串缺少 AI 21" in body["errors"][0]["reason"]
    assert "子件扫描串缺少 AI 21" in body["errors"][1]["reason"]
    # 第二次操作父件 C 解析合法,但因子件失败而整体失败;C 不在成功操作中
    assert body["items"] == []


def test_failed_operations_do_not_block_later_ones():
    p1 = scan(GTIN, "P1")
    c1 = scan(GTIN2, "C1")
    c2 = scan(GTIN3, "C2")
    body = post(
        [
            pack(p1, c1),
            pack(scan(GTIN4, "X"), c1),  # C1 已有父件 → 失败
            pack(p1, c2),  # 后续操作照常处理
        ]
    )
    assert [e["index"] for e in body["errors"]] == [1]
    im = item_map(body)
    assert im[(GTIN2, "C1")]["parent"] == {"gtin": GTIN, "serial": "P1"}
    assert im[(GTIN3, "C2")]["parent"] == {"gtin": GTIN, "serial": "P1"}
    assert (GTIN4, "X") not in im


def test_errors_sorted_by_original_index():
    p = scan(GTIN, "P")
    c = scan(GTIN2, "C")
    body = post(
        [
            pack(p, c),
            unpack(p, c),
            unpack(p, c),  # 已取出,再取一次失败
            pack(p, c),
            pack(c, p),  # P 已是 C 的子件,装入祖先成环
        ]
    )
    assert [e["index"] for e in body["errors"]] == [2, 4]


def test_same_scan_identity_equal_across_other_ais():
    # 批号 / 有效期不影响实物身份:同 GTIN+序列号即同一实物
    p = scan(GTIN, "P")
    c1 = scan(GTIN2, "C", batch="L1")
    c2 = scan(GTIN2, "C", batch="L2", expiry="260101")  # 同一实物的另一种扫描串
    body = post([pack(p, c1), unpack(p, c2)])
    assert body["errors"] == []
    im = item_map(body)
    assert im[(GTIN2, "C")]["parent"] is None


# ---------- 请求体验证(422) ----------

def _expect_422(payload):
    resp = client.post("/v1/packing/audit", json=payload)
    assert resp.status_code == 422, resp.text


def test_validation_operation_count():
    o = pack(scan(GTIN, "P"), scan(GTIN2, "C"))
    _expect_422({"operations": []})
    _expect_422({"operations": [o] * 301})
    resp = client.post("/v1/packing/audit", json={"operations": [o] * 300})
    assert resp.status_code == 200, resp.text
    # 300 条里只有第 0 条成功,其余 299 条全部"重复装入"失败
    body = resp.json()
    assert len(body["items"]) == 2
    assert len(body["errors"]) == 299


def test_validation_action_enum():
    _expect_422(
        {
            "operations": [
                {"action": "move", "parent_scan": scan(GTIN, "P"), "child_scan": scan(GTIN2, "C")}
            ]
        }
    )


def test_validation_required_fields_and_no_extra():
    _expect_422({"operations": [{"parent_scan": scan(GTIN, "P"), "child_scan": scan(GTIN2, "C")}]})
    _expect_422({"operations": [{"action": "pack", "child_scan": scan(GTIN2, "C")}]})
    _expect_422({"operations": [{"action": "pack", "parent_scan": scan(GTIN, "P")}]})
    _expect_422(
        {
            "operations": [
                dict(
                    pack(scan(GTIN, "P"), scan(GTIN2, "C")),
                    extra=1,
                )
            ]
        }
    )


def test_existing_endpoints_still_available():
    assert client.get("/healthz").json() == {"status": "ok"}
    resp = client.post(
        "/v1/custody/audit",
        json={
            "events": [
                {
                    "scan": scan(GTIN, "SN1"),
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "from_location": None,
                    "to_location": "WH-A",
                }
            ]
        },
    )
    assert resp.status_code == 200
