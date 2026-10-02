"""交接日志最少剔除:在现有交接事件中选择保留子序列,使剔除总数最少。

复用 gs1.parse_scan 与 CustodyEvent 校验;事件按 occurred_at 转 UTC 升序处理,
同一时刻依输入顺序,实物由 GTIN + AI 21 序列号唯一标识。

规则:

- 扫描非法或缺 AI 21 序列号的事件必须剔除;被 locked_indices 锁定时整单 422。
- 每件实物的保留事件构成合法交接链:首条 from_location 为 null,
  后续事件 from_location 等于上一条保留事件的 to_location,
  批号(AI 10)、有效期(AI 17,含缺失)与首条保留事件一致。
- locked_indices 指向的事件必须保留;无法同时保留(被强制剔除或没有可行子序列)
  时整单 422。
- 目标:剔除总数最少;并列时,按处理顺序排列的「保留原下标序列」取字典序最小。

各实物的子序列虽相互独立,但字典序比较的是合并后的保留原下标序列,逐实物各自
取字典序最小并不等价于全局字典序最小(处理顺序与原下标顺序可能交错)。

算法:

1. 为每件实物预处理后缀 DP 表 F[u][j]:最后保留事件局部下标为 u(-1 尚无)、
   从局部下标 j 起(已锁定事件必须保留)最多还能保留几条,不可行记为 -inf。
2. 把各实物的 F 列投影到全局处理位置:col[k][p] 表示在游标 p 处实物 k 的后缀
   贡献;total[p] 汇总各实物贡献,infeas[p] 统计锁定实物中不可行者。
3. 全局逐槽贪心:扫描到下一个锁定位置为止的候选,选出仍能达到最大保留总数、
   且原下标最小的处理位置;选中后只重算该实物一列(O(m))并增量刷新汇总。
   总复杂度 O(Σn_k² + m² + 事件解析),m ≤ 300。
"""

from __future__ import annotations

from datetime import date as dt_date
from datetime import timezone

from fastapi import HTTPException

from .gs1 import ScanParseError, parse_scan
from .models import CustodyTrimItem, CustodyTrimRequest, CustodyTrimResponse

_INFEASIBLE = -10**9  # 小于任何合法保留数(0~300)


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(status_code=422, detail=reason)


class _ItemTable:
    """单件实物的事件序列与后缀 DP 表。"""

    def __init__(
        self,
        nodes: list[tuple[int, str, str | None, str, str | None, dt_date | None]],
        locked_local: set[int],
    ) -> None:
        # node:(全局处理位置, 原下标, from_location, to_location, 批号, 有效期)
        self.nodes = nodes
        self.locked_local = locked_local
        n = len(nodes)
        self.n = n
        suffix_locked = [False] * (n + 1)
        for j in range(n - 1, -1, -1):
            suffix_locked[j] = j in locked_local or suffix_locked[j + 1]
        self.suffix_locked = suffix_locked

        # F[u+1][j]:最后保留局部下标 u(-1..n-1)、从 j 起的后缀最大保留数
        table = [[_INFEASIBLE] * (n + 1) for _ in range(n + 1)]
        for row in table:
            row[n] = 0
        for j in range(n - 1, -1, -1):
            for u in range(-1, j):
                best = 0 if not suffix_locked[j] else _INFEASIBLE
                if j not in locked_local:
                    # 跳过 j(锁定事件不允许跳过)
                    skipped = table[u + 1][j + 1]
                    if skipped > best:
                        best = skipped
                if self._compatible(u, j):
                    tail = table[j + 1][j + 1]
                    if tail > _INFEASIBLE:
                        best = max(best, 1 + tail)
                table[u + 1][j] = best
        self.table = table
        # 各局部下标对应的全局处理位置(严格升序)
        self.gpos = [node[0] for node in nodes]

    def _compatible(self, u: int, j: int) -> bool:
        """局部下标 j 能否接在最后保留事件 u 之后(首条要求 from_location 为 null)。"""
        _, _, frm_j, _, batch_j, expiry_j = self.nodes[j]
        if u == -1:
            return frm_j is None
        _, _, _, to_u, batch_u, expiry_u = self.nodes[u]
        return frm_j == to_u and batch_j == batch_u and expiry_j == expiry_u


def trim_custody(request: CustodyTrimRequest) -> CustodyTrimResponse:
    """选择剔除总数最少(并列时保留序列字典序最小)的保留子序列。"""
    events = request.events
    count = len(events)
    locked_orig = set(request.locked_indices)

    # 处理顺序:UTC 升序,同刻输入顺序
    order = sorted(
        range(count),
        key=lambda i: (events[i].occurred_at.astimezone(timezone.utc), i),
    )

    # 解析:非法 / 缺序列号的事件强制剔除(被锁定则整单 422),不进入任何实物
    valid_nodes: dict[tuple[str, str], list[int]] = {}
    parsed_at: dict[int, tuple[str, str, str | None, dt_date | None]] = {}
    for pos, index in enumerate(order):
        event = events[index]
        try:
            parsed = parse_scan(event.scan)
        except ScanParseError as exc:
            if index in locked_orig:
                raise _unprocessable(
                    f"锁定事件 {index} 扫描非法,无法保留: {exc.reason}"
                ) from exc
            continue
        if parsed.serial is None:
            if index in locked_orig:
                raise _unprocessable(
                    f"锁定事件 {index} 扫描串缺少 AI 21 序列号,无法识别实物,无法保留"
                )
            continue
        parsed_at[pos] = (parsed.gtin, parsed.serial, parsed.batch, parsed.expiry)
        valid_nodes.setdefault((parsed.gtin, parsed.serial), []).append(pos)

    m = len(order)
    # 全局处理位置 -> 所属实物与局部下标
    pos_item: dict[int, tuple[str, str]] = {}
    pos_local: dict[int, int] = {}
    locked_positions = {
        pos for pos, index in enumerate(order) if index in locked_orig
    }

    keys = sorted(valid_nodes)
    kcount = len(keys)
    kid = {key: k for k, key in enumerate(keys)}
    table_list: list[_ItemTable] = []
    has_lock: list[bool] = []
    for key in keys:
        positions = valid_nodes[key]
        nodes = []
        locked_local: set[int] = set()
        for local, pos in enumerate(positions):
            index = order[pos]
            _gtin, _serial, batch, expiry = parsed_at[pos]
            event = events[index]
            nodes.append((pos, index, event.from_location, event.to_location, batch, expiry))
            if pos in locked_positions:
                locked_local.add(local)
            pos_item[pos] = key
            pos_local[pos] = local
        table = _ItemTable(nodes, locked_local)
        if locked_local and table.table[0][0] <= _INFEASIBLE:
            locked_index = order[positions[min(locked_local)]]
            raise _unprocessable(
                f"锁定事件(如下标 {locked_index})无法与该实物的其他锁定事件同时保留,不存在合法保留子序列"
            )
        table_list.append(table)
        has_lock.append(bool(locked_local))

    # 最大保留总数(无锁定的实物可整体剔除,其不可行贡献按 0 计)
    total_best = sum(max(0, table.table[0][0]) for table in table_list)

    # jmap[k][p]:实物 k 首个全局处理位置 >= p 的事件局部下标(p 取 0..m),都更小时为 n。
    # 各实物位置本就严格升序,线性归约一次,后续贪心不再做二分。
    jmap: list[list[int]] = []
    for table in table_list:
        mapping = [0] * (m + 1)
        j = 0
        for p in range(m + 1):
            while j < table.n and table.gpos[j] < p:
                j += 1
            mapping[p] = j
        jmap.append(mapping)

    # state[k]:实物 k 最后保留事件的局部下标(-1 尚无)。
    # col[k][p] = F[state[k]+1][ jmap[k][p] ]:游标在 p 处时实物 k 的后缀最大保留数。
    # total[p] 汇总可行贡献(无锁定实物不可行按 0),infeas[p] 统计锁定实物中不可行者。
    state = [-1] * kcount
    col: list[list[int]] = [
        [table.table[0][mapping[p]] for p in range(m + 1)]
        for table, mapping in zip(table_list, jmap)
    ]
    total = [0] * (m + 1)
    infeas = [0] * (m + 1)
    for k in range(kcount):
        column = col[k]
        locked = has_lock[k]
        for p in range(m + 1):
            value_p = column[p]
            if value_p > _INFEASIBLE:
                total[p] += value_p
            elif locked:
                infeas[p] += 1

    def update_picked(k: int, new_state: int) -> None:
        """实物 k 选中后状态变为 new_state:仅重算它这一列并增量刷新 total/infeas。"""
        table = table_list[k]
        mapping = jmap[k]
        column = col[k]
        row = new_state + 1
        locked = has_lock[k]
        for p in range(m + 1):
            old = column[p]
            new = table.table[row][mapping[p]]
            column[p] = new
            if old > _INFEASIBLE:
                total[p] -= old
            elif locked:
                infeas[p] -= 1
            if new > _INFEASIBLE:
                total[p] += new
            elif locked:
                infeas[p] += 1

    # 每个处理位置之后(含自身)最近的锁定位置;无则哨兵 m
    next_lock = [m] * (m + 1)
    nxt = m
    for pos in range(m - 1, -1, -1):
        if pos in locked_positions:
            nxt = pos
        next_lock[pos] = nxt

    # 全局逐槽贪心:在每段(到下一个锁定位置为止)候选中,选出仍能达到最大保留
    # 总数、且原下标最小的处理位置。
    kept_orig: list[int] = []
    cursor = 0
    remaining = total_best

    while remaining > 0:
        limit = next_lock[cursor]
        upper = limit if limit < m else m - 1
        best_pos: int | None = None
        best_label: int | None = None
        for pos in range(cursor, upper + 1):
            key = pos_item.get(pos)
            if key is None:
                continue  # 强制剔除位置
            k = kid[key]
            local = pos_local[pos]
            table = table_list[k]
            if not table._compatible(state[k], local):
                continue
            tail = table.table[local + 1][local + 1]
            if tail <= _INFEASIBLE:
                continue
            threshold = pos + 1
            # 除本实物外,其余锁定实物在 threshold 处必须可行
            others_infeas = infeas[threshold] - (
                1 if has_lock[k] and col[k][threshold] <= _INFEASIBLE else 0
            )
            if others_infeas != 0:
                continue
            own_current = col[k][threshold]
            if own_current <= _INFEASIBLE:
                own_current = 0
            achievable = 1 + tail + total[threshold] - own_current
            if achievable != remaining:
                continue
            label = order[pos]
            if best_label is None or label < best_label:
                best_label = label
                best_pos = pos
        if best_pos is None:
            # 理论上不可达:锁定可行性与最大保留数已预先保证
            raise _unprocessable("锁定事件无法同时保留,不存在满足约束的保留子序列")
        key = pos_item[best_pos]  # type: ignore[index]
        k = kid[key]
        local = pos_local[best_pos]
        kept_orig.append(order[best_pos])
        update_picked(k, local)
        state[k] = local
        cursor = best_pos + 1
        remaining -= 1

    kept_set = set(kept_orig)
    # 最终持有人:每件实物最后一条保留事件(处理顺序)的 to_location
    holder_of: dict[tuple[str, str], str] = {}
    for k, key in enumerate(keys):
        if state[k] >= 0:
            holder_of[key] = table_list[k].nodes[state[k]][3]

    items = [
        CustodyTrimItem(gtin=gtin, serial=serial, holder=holder_of[(gtin, serial)])
        for gtin, serial in keys
        if (gtin, serial) in holder_of
    ]
    removed_indices = sorted(index for index in range(count) if index not in kept_set)
    return CustodyTrimResponse(
        kept_indices=kept_orig,
        removed_indices=removed_indices,
        items=items,
    )
