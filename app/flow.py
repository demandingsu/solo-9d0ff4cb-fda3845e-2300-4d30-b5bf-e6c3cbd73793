"""纯函数：基于最大流的核定户数相容性判定与逐格整数流向求解。

业务问题：外部核定的是每个**片区**的户数，而旧网格的户数挂在**旧格**上；
旧格的户数只能分给与其矩形面积相交的片区，不必沿用面积比例。
这等价于一个带容量的整数运输问题：

    源点 S --(容量=旧格户数)--> 旧格 --(容量=无穷)--> 相交片区
         --(容量=片区核定户数)--> 汇点 T

- 所有容量均为整数，Dinic 求出来的最大流也是整数，直接得到逐格 -> 逐片区的整数流向；
- 最大流等于总户数即相容（每个旧格供完、每个片区恰好满足核定）；
- 不可行时，残余网络中源点可达的旧格即“受限旧格”，它们能到达的片区即
  “可达片区”，总户数与最大流之差即“缺额”（Hall 型诊断的最小割见证）。

本模块不依赖数据库与 Web 层，几何相交关系由调用方计算后以边列表传入。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


class _Edge:
    """Dinic 残余网络边。cap 为残余容量，rev 指向反向边在邻接表中的下标。"""

    __slots__ = ("to", "rev", "cap", "inf")

    def __init__(self, to: int, rev: int, cap: int, inf: bool = False):
        self.to = to
        self.rev = rev
        self.cap = cap
        # 旧格 -> 片区的无穷容量边，需要靠它回收原始容量以读取流量
        self.inf = inf


@dataclass(frozen=True)
class CellFlow:
    """单个旧格流向单个片区的整数户数。"""

    old_id: str
    district_id: str
    households: int


@dataclass(frozen=True)
class FeasibilityResult:
    """相容性判定结果。

    feasible=True 时 flows 为全部正流量整数流向（两侧合计各自吻合）；
    feasible=False 时 restricted_old_ids/reachable_district_ids/shortfall 为诊断信息。
    """

    feasible: bool
    total: int
    max_flow: int
    flows: list[CellFlow]
    restricted_old_ids: list[str]
    reachable_district_ids: list[str]
    shortfall: int


class _Dinic:
    def __init__(self, n: int):
        self.n = n
        self.graph: list[list[_Edge]] = [[] for _ in range(n)]
        self.level = [-1] * n
        self.it = [0] * n

    def add_edge(self, fr: int, to: int, cap: int, inf: bool = False) -> tuple[int, int]:
        """加一条容量为 cap 的边，返回 (邻接表下标, 原始容量) 便于事后读流量。"""
        forward = _Edge(to, len(self.graph[to]), cap, inf)
        backward = _Edge(fr, len(self.graph[fr]), 0)
        self.graph[fr].append(forward)
        self.graph[to].append(backward)
        return len(self.graph[fr]) - 1, cap

    def _bfs(self, s: int, t: int) -> bool:
        self.level = [-1] * self.n
        self.level[s] = 0
        queue = deque([s])
        while queue:
            v = queue.popleft()
            for e in self.graph[v]:
                if e.cap > 0 and self.level[e.to] < 0:
                    self.level[e.to] = self.level[v] + 1
                    queue.append(e.to)
        return self.level[t] >= 0

    def _dfs(self, v: int, t: int, pushed: int) -> int:
        if v == t:
            return pushed
        while self.it[v] < len(self.graph[v]):
            e = self.graph[v][self.it[v]]
            if e.cap > 0 and self.level[e.to] == self.level[v] + 1:
                d = self._dfs(e.to, t, min(pushed, e.cap))
                if d > 0:
                    e.cap -= d
                    self.graph[e.to][e.rev].cap += d
                    return d
            self.it[v] += 1
        return 0

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        while self._bfs(s, t):
            self.it = [0] * self.n
            while True:
                f = self._dfs(s, t, 10**18)
                if f == 0:
                    break
                flow += f
        return flow

    def reachable_from(self, s: int) -> set[int]:
        """最大流跑完后：沿残余容量为正的边，从 s 可达的全部节点。"""
        seen = {s}
        queue = deque([s])
        while queue:
            v = queue.popleft()
            for e in self.graph[v]:
                if e.cap > 0 and e.to not in seen:
                    seen.add(e.to)
                    queue.append(e.to)
        return seen


def solve_transport(
    old_cells: list[tuple[str, int]],
    districts: list[tuple[str, int]],
    overlap_edges: list[tuple[str, str]],
) -> FeasibilityResult:
    """判定片区核定户数能否由旧格户数只沿相交关系整数分摊。

    old_cells:      (旧格编号, 户数供给) 列表，编号唯一；
    districts:      (片区编号, 核定户数需求) 列表，编号唯一；
    overlap_edges:  面积相交的 (旧格编号, 片区编号) 对（只需是否相交，与面积大小无关）。

    容量均为非负整数，返回的流量也是整数。调用方负责保证两侧总量相同；
    本函数不强制要求总量相等，总量不等时自然判定为不可行并给出缺额。
    """
    old_ids = [oid for oid, _ in old_cells]
    district_ids = [did for did, _ in districts]
    if len(set(old_ids)) != len(old_ids):
        raise ValueError("旧格编号必须唯一")
    if len(set(district_ids)) != len(district_ids):
        raise ValueError("片区编号必须唯一")
    if any(supply < 0 for _, supply in old_cells):
        raise ValueError("旧格户数必须非负")
    if any(demand < 0 for _, demand in districts):
        raise ValueError("片区核定户数必须非负")

    old_index = {oid: i for i, oid in enumerate(old_ids)}
    district_index = {did: i for i, did in enumerate(district_ids)}

    m, n = len(old_ids), len(district_ids)
    source = 0
    old_base = 1
    district_base = old_base + m
    sink = district_base + n
    dinic = _Dinic(sink + 1)

    total_supply = sum(supply for _, supply in old_cells)
    # 无穷容量取总供给即可：任何单条边的实际流量都不可能超过总户数
    inf_cap = max(1, total_supply)

    # 边的加入顺序固定（旧格顺序、片区顺序），保证 Dinic 结果确定可复现
    for oid, supply in old_cells:
        dinic.add_edge(source, old_base + old_index[oid], supply)

    # 记录旧格 -> 片区边，用于事后读取整数流量
    edge_refs: list[tuple[str, str, int, int]] = []
    edge_pairs = set(overlap_edges)
    for oid in old_ids:
        for did in district_ids:
            if (oid, did) in edge_pairs:
                idx, original_cap = dinic.add_edge(
                    old_base + old_index[oid],
                    district_base + district_index[did],
                    inf_cap,
                    inf=True,
                )
                edge_refs.append((oid, did, idx, original_cap))

    for did, demand in districts:
        dinic.add_edge(district_base + district_index[did], sink, demand)

    max_flow_value = dinic.max_flow(source, sink)
    feasible = max_flow_value == total_supply

    flows: list[CellFlow] = []
    if feasible:
        for oid, did, idx, original_cap in edge_refs:
            remaining = dinic.graph[old_base + old_index[oid]][idx].cap
            used = original_cap - remaining
            if used > 0:
                flows.append(CellFlow(old_id=oid, district_id=did, households=used))

        # 守恒护栏：逐格供给、逐片区需求都必须恰好吻合
        per_old = {oid: 0 for oid in old_ids}
        per_district = {did: 0 for did in district_ids}
        for f in flows:
            per_old[f.old_id] += f.households
            per_district[f.district_id] += f.households
        if per_old != dict(old_cells):
            raise RuntimeError("内部错误：旧格侧流量与供给不一致")
        if per_district != dict(districts):
            raise RuntimeError("内部错误：片区侧流量与核定需求不一致")

    reachable = dinic.reachable_from(source)
    restricted = [
        old_ids[i]
        for i in range(m)
        if old_base + i in reachable
    ]
    reachable_districts = [
        district_ids[i]
        for i in range(n)
        if district_base + i in reachable
    ]
    shortfall = total_supply - max_flow_value

    return FeasibilityResult(
        feasible=feasible,
        total=total_supply,
        max_flow=max_flow_value,
        flows=flows,
        restricted_old_ids=restricted,
        reachable_district_ids=reachable_districts,
        shortfall=shortfall,
    )
