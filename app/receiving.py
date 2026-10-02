"""到货核对:把实扫 GS1 扫描串核销到期望行名额。

流程:

1. 复用 gs1.parse_scan 逐条解析;解析失败的扫描按原下标返回偏移与原因,
   不参与后续处理。
2. 含 AI 21 的扫描按 GTIN + 序列号全局去重(按扫描原顺序):同一实物只计首次,
   后续重复项单列,不参与分配。
3. 每条有效扫描最多占用一个名额:GTIN 相等,且期望行附带的每个限定字段
   (批号 / 序列号 / 到期日)在扫描串中均存在且相等。
4. 全局分配在可行名额约束下依次优化:
   a. 核销数(已分配名额数)最多;
   b. 已分配名额的限定字段数总和最大(优先核销更具体的期望行);
   c. 仍并列时,按扫描原顺序取期望行下标序列字典序最小,未分配视为大于所有行。

实现上把三级目标编码为整数费用,以最小费用最大流(SPFA 逐次最短路)一次性求最优:
未分配走容量充足的哑边,使每条可分配扫描都恰好流过 1 个单位,故最大流固定,
费用最小即全局最优。
"""

from __future__ import annotations

from collections import deque

from .gs1 import ParsedScan, ScanParseError, parse_scan
from .models import (
    ExpectedLine,
    ReceivingAssignment,
    ReceivingLineRemaining,
    ReceivingReconcileRequest,
    ReceivingReconcileResponse,
    ReceivingScanError,
)


def _line_spec_count(line: ExpectedLine) -> int:
    """期望行附带的限定字段数(0~3)。"""
    return (line.batch is not None) + (line.serial is not None) + (line.expiry is not None)


def _compatible(parsed: ParsedScan, line: ExpectedLine) -> bool:
    """扫描能否占用该期望行名额:GTIN 相等且行内每个限定字段在扫描中存在且相等。"""
    if parsed.gtin != line.gtin:
        return False
    if line.batch is not None and parsed.batch != line.batch:
        return False
    if line.serial is not None and parsed.serial != line.serial:
        return False
    if line.expiry is not None and parsed.expiry != line.expiry:
        return False
    return True


class _MinCostFlow:
    """邻接表最小费用最大流;容量为小整数,费用可为大整数,SPFA 逐次增广。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.graph: list[list[dict[str, int]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> int:
        """加边并返回正向边在 graph[u] 中的下标(便于事后读流量)。"""
        index = len(self.graph[u])
        self.graph[u].append({"to": v, "cap": cap, "cost": cost, "rev": len(self.graph[v])})
        self.graph[v].append({"to": u, "cap": 0, "cost": -cost, "rev": index})
        return index

    def min_cost_flow(self, source: int, sink: int, required: int) -> int:
        """推送 required 个单位流,返回最小总费用(题目保证可推送:哑边容量充足)。"""
        inf = 10 ** 1000
        total_cost = 0
        flow = 0
        while flow < required:
            dist = [inf] * self.n
            prev_v = [-1] * self.n
            prev_e = [-1] * self.n
            in_queue = [False] * self.n
            dist[source] = 0
            queue = deque([source])
            in_queue[source] = True
            while queue:
                u = queue.popleft()
                in_queue[u] = False
                for ei, edge in enumerate(self.graph[u]):
                    if edge["cap"] > 0 and dist[u] + edge["cost"] < dist[edge["to"]]:
                        dist[edge["to"]] = dist[u] + edge["cost"]
                        prev_v[edge["to"]] = u
                        prev_e[edge["to"]] = ei
                        if not in_queue[edge["to"]]:
                            queue.append(edge["to"])
                            in_queue[edge["to"]] = True
            assert dist[sink] < inf, "无法达到要求流量:哑边应保证始终可增广"
            # 最短路每次至少增广 1;直接推 1 个单位即可(required ≤ 60)
            v = sink
            while v != source:
                edge = self.graph[prev_v[v]][prev_e[v]]
                edge["cap"] -= 1
                self.graph[v][edge["rev"]]["cap"] += 1
                v = prev_v[v]
            total_cost += dist[sink]
            flow += 1
        return total_cost


def reconcile(request: ReceivingReconcileRequest) -> ReceivingReconcileResponse:
    """解析扫描、去重并做全局最优分配,返回核销结果与各类下标。"""
    lines = request.lines
    m = len(lines)

    # 1. 解析 + 2. 含 AI 21 按 GTIN+序列号去重(仅对解析成功者)
    errors: list[ReceivingScanError] = []
    candidates: list[tuple[int, ParsedScan]] = []  # (原下标, 解析结果):有效且非重复
    duplicate_indices: list[int] = []
    seen_serials: set[tuple[str, str]] = set()

    for index, raw in enumerate(request.scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            errors.append(
                ReceivingScanError(index=index, offset=exc.offset, reason=exc.reason)
            )
            continue
        if parsed.serial is not None:
            key = (parsed.gtin, parsed.serial)
            if key in seen_serials:
                duplicate_indices.append(index)
                continue
            seen_serials.add(key)
        candidates.append((index, parsed))

    k = len(candidates)

    # 费用编码基数:严格大于每位数字的最大值(行下标最大 m-1,未分配为 m)
    base = m + 1
    max_specs = max((_line_spec_count(line) for line in lines), default=0)
    # 字典序项权重(按扫描原顺序,越靠前位权越大)
    lex_weights = [base ** (k - 1 - pos) for pos in range(k)]
    # W 项须压过字典序项的最大差;F 项须压过 W 项的最大差,留足余量
    lex_span = base ** k
    c_w = lex_span + 1  # 每核销 1 个限定字段的权重
    c_f = c_w * (k * max(1, max_specs) + 1)  # 每核销 1 条扫描的权重

    # 建图:source -> 候选扫描(每点 1 流) -> 兼容期望行(容量 quantity) -> sink;
    # 扫描 -> sink 的未分配哑边容量 1,保证每条候选扫描都恰好流过 1 个单位。
    source = 0
    scan_nodes = [1 + i for i in range(k)]
    line_nodes = [1 + k + j for j in range(m)]
    sink = 1 + k + m
    mcf = _MinCostFlow(sink + 1)

    assignment_edges: list[list[tuple[int, int]]] = []  # 每条候选扫描的全部行边 (u, edge_index)
    for pos, ((index, parsed), scan_node) in enumerate(zip(candidates, scan_nodes)):
        mcf.add_edge(source, scan_node, 1, 0)
        edges: list[tuple[int, int]] = []
        for j, line in enumerate(lines):
            if _compatible(parsed, line):
                ei = mcf.add_edge(
                    scan_node,
                    line_nodes[j],
                    1,
                    # 分配:−F − W + 字典序位(行下标越小越优)
                    -c_f - _line_spec_count(line) * c_w + lex_weights[pos] * j,
                )
                edges.append((scan_node, ei))
        # 未分配哑边:字典序哨兵 m(大于所有行下标 0..m-1)
        mcf.add_edge(scan_node, sink, 1, lex_weights[pos] * m)
        assignment_edges.append(edges)

    for j, line in enumerate(lines):
        mcf.add_edge(line_nodes[j], sink, line.quantity, 0)

    mcf.min_cost_flow(source, sink, k)

    # 从正向边残余容量读取每条候选扫描的去向(每点恰有一条行边容量被用掉)
    assigned_line: dict[int, int] = {}
    for pos, (index, _parsed) in enumerate(candidates):
        for u, ei in assignment_edges[pos]:
            edge = mcf.graph[u][ei]
            if edge["cap"] == 0:  # 容量被用掉 1 => 该扫描走了这条边
                assigned_line[index] = edge["to"] - (1 + k)  # 反查行下标
                break

    assignments: list[ReceivingAssignment] = []
    candidate_indices = {index for index, _ in candidates}
    for index in range(len(request.scans)):
        line_index = assigned_line.get(index)
        if line_index is not None:
            assignments.append(
                ReceivingAssignment(index=index, line_id=lines[line_index].id)
            )
        else:
            assignments.append(ReceivingAssignment(index=index, line_id=None))

    unassigned_indices = sorted(
        index for index in candidate_indices if index not in assigned_line
    )

    # 用流量统计每行业务核销数,得出剩余数量
    used = [0] * m
    for index, line_index in assigned_line.items():
        used[line_index] += 1
    line_remaining = [
        ReceivingLineRemaining(id=line.id, remaining=line.quantity - used[j])
        for j, line in enumerate(lines)
    ]

    errors.sort(key=lambda error: error.index)
    return ReceivingReconcileResponse(
        assignments=assignments,
        errors=errors,
        unassigned_indices=unassigned_indices,
        duplicate_indices=duplicate_indices,
        lines=line_remaining,
    )
