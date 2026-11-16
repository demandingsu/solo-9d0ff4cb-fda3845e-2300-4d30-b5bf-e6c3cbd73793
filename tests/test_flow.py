"""最大流相容性判定纯逻辑测试。

覆盖：可行整数流向与双侧守恒、不按面积比例的自由分配、总量相同却不可行的
受限旧格/可达片区/缺额诊断、零户数边界、确定性复现，以及随机运输问题的
可行性结论与独立判定互相印证。
"""
from __future__ import annotations

import random

from app.flow import CellFlow, solve_transport


def _sums(result):
    # 零供给/零需求不产生正流量边，汇总时补齐全部编号以便与输入对照
    per_old: dict[str, int] = {}
    per_district: dict[str, int] = {}
    for f in result.flows:
        per_old[f.old_id] = per_old.get(f.old_id, 0) + f.households
        per_district[f.district_id] = (
            per_district.get(f.district_id, 0) + f.households
        )
    return per_old, per_district


def test_feasible_integer_flow_meets_both_sides():
    # g1=6 只到 d1；g2=4 可到 d1/d2。需求 d1=7、d2=3，总量 10。
    result = solve_transport(
        old_cells=[("g1", 6), ("g2", 4)],
        districts=[("d1", 7), ("d2", 3)],
        overlap_edges=[("g1", "d1"), ("g2", "d1"), ("g2", "d2")],
    )
    assert result.feasible is True
    assert result.max_flow == 10
    assert result.shortfall == 0
    assert result.restricted_old_ids == []
    assert result.reachable_district_ids == []
    per_old, per_district = _sums(result)
    assert per_old == {"g1": 6, "g2": 4}
    assert per_district == {"d1": 7, "d2": 3}
    # g1 只能给 d1
    g1_flows = [f for f in result.flows if f.old_id == "g1"]
    assert g1_flows == [CellFlow("g1", "d1", 6)]
    # 所有流量都是非负整数
    assert all(isinstance(f.households, int) and f.households > 0 for f in result.flows)


def test_flow_not_forced_to_area_ratio():
    # 几何上 g2 与两片区等面积相交，但核定允许任意整数拆分：
    # d1 需要 4、d2 需要 0 时，g2 的 4 户可全部流向 d1。
    result = solve_transport(
        old_cells=[("g1", 6), ("g2", 4)],
        districts=[("d1", 10), ("d2", 0)],
        overlap_edges=[("g1", "d1"), ("g2", "d1"), ("g2", "d2")],
    )
    assert result.feasible is True
    _, per_district = _sums(result)
    assert per_district == {"d1": 10}
    assert not any(f.district_id == "d2" for f in result.flows)
    # g2 的 4 户全部给 d1；d2 核定为 0，不产生流向它的边
    g2 = {f.district_id: f.households for f in result.flows if f.old_id == "g2"}
    assert g2 == {"d1": 4}


def test_infeasible_diagnosis_restricted_reachable_shortfall():
    # g1 只接触 d1，却要供 6；d1 仅核定 3。g2 的 4 户无法穿过 g1 去补 d1。
    result = solve_transport(
        old_cells=[("g1", 6), ("g2", 4)],
        districts=[("d1", 3), ("d2", 7)],
        overlap_edges=[("g1", "d1"), ("g2", "d1"), ("g2", "d2")],
    )
    assert result.feasible is False
    assert result.max_flow == 7
    assert result.shortfall == 3
    assert result.restricted_old_ids == ["g1"]
    assert result.reachable_district_ids == ["d1"]
    assert result.flows == []


def test_infeasible_witness_with_two_restricted_cells():
    # g1、g2 都只能到 d1；g3 可到 d1/d2。
    # d1=2、d2=6，总量 12。受限集合 {g1,g2}（合计 8），可达片区 {d1}（仅 2），缺额 6。
    result = solve_transport(
        old_cells=[("g1", 5), ("g2", 3), ("g3", 4)],
        districts=[("d1", 2), ("d2", 6)],
        overlap_edges=[
            ("g1", "d1"),
            ("g2", "d1"),
            ("g3", "d1"),
            ("g3", "d2"),
        ],
    )
    assert result.feasible is False
    assert result.shortfall == 6
    assert result.restricted_old_ids == ["g1", "g2"]
    assert result.reachable_district_ids == ["d1"]


def test_zero_households_and_zero_demand_are_feasible():
    result = solve_transport(
        old_cells=[("g1", 0), ("g2", 0)],
        districts=[("d1", 0), ("d2", 0)],
        overlap_edges=[("g1", "d1"), ("g2", "d1"), ("g2", "d2")],
    )
    assert result.feasible is True
    assert result.max_flow == 0
    assert result.flows == []
    assert result.shortfall == 0


def test_cell_with_no_overlap_is_blocked_when_it_has_households():
    # g1 与任何片区都不相交且有户数 -> 整户成为缺额，g1 被诊断为受限旧格
    result = solve_transport(
        old_cells=[("g1", 5), ("g2", 5)],
        districts=[("d1", 10)],
        overlap_edges=[("g2", "d1")],
    )
    assert result.feasible is False
    assert result.shortfall == 5
    assert result.restricted_old_ids == ["g1"]
    assert result.reachable_district_ids == []


def test_deterministic_result_across_runs():
    kwargs = dict(
        old_cells=[("g1", 6), ("g2", 4), ("g3", 8)],
        districts=[("d1", 7), ("d2", 5), ("d3", 6)],
        overlap_edges=[
            ("g1", "d1"),
            ("g2", "d1"),
            ("g2", "d2"),
            ("g3", "d2"),
            ("g3", "d3"),
        ],
    )
    first = solve_transport(**kwargs)
    for _ in range(5):
        again = solve_transport(**kwargs)
        assert again.feasible == first.feasible
        assert [(f.old_id, f.district_id, f.households) for f in again.flows] == [
            (f.old_id, f.district_id, f.households) for f in first.flows
        ]


def _greedy_feasible(supplies, demands, adjacency):
    """独立的回溯判定器，只判断“是否可行”，用于随机交叉验证最大流结论。"""
    cells = list(supplies)
    remaining_supply = dict(supplies)
    remaining_demand = dict(demands)

    def backtrack(i: int) -> bool:
        if i == len(cells):
            return all(v == 0 for v in remaining_demand.values())
        cid, _ = cells[i]
        options = [d for d in adjacency[cid] if remaining_demand[d] > 0]

        def distribute(j: int, budget: int) -> bool:
            if j == len(options):
                return budget == 0 and backtrack(i + 1)
            did = options[j]
            cap = min(budget, remaining_demand[did])
            for x in range(cap + 1):
                remaining_demand[did] -= x
                if distribute(j + 1, budget - x):
                    return True
                remaining_demand[did] += x
            return False

        return distribute(0, remaining_supply[cid])

    return backtrack(0)


def test_random_instances_match_independent_checker():
    rng = random.Random(20261116)
    for _ in range(200):
        n_cells = rng.randint(1, 5)
        n_districts = rng.randint(1, 5)
        supplies = [(f"g{i}", rng.randint(0, 9)) for i in range(n_cells)]
        total = sum(s for _, s in supplies)
        # 随机切出与总供给相等的非负需求（星与条：在 total+n-1 个位置选 n-1 个隔板）
        if n_districts == 1:
            demands = [("d0", total)]
        else:
            separators = sorted(
                rng.sample(range(total + n_districts - 1), n_districts - 1)
            )
            bounds = [-1, *separators, total + n_districts - 1]
            demands = [
                ("d" + str(j), bounds[j + 1] - bounds[j] - 1)
                for j in range(n_districts)
            ]
        adjacency = {
            cid: [did for did, _ in demands if rng.random() < 0.6]
            for cid, _ in supplies
        }
        edges = [(cid, did) for cid, ids in adjacency.items() for did in ids]

        result = solve_transport(supplies, demands, edges)
        expected = _greedy_feasible(supplies, demands, adjacency)
        assert result.feasible is expected
        if expected:
            per_old, per_district = _sums(result)
            # 零供给旧格/零需求片区没有正流量边，补齐零后再对照
            for cid, supply in supplies:
                assert per_old.get(cid, 0) == supply
            for did, demand in demands:
                assert per_district.get(did, 0) == demand
        else:
            assert result.shortfall == total - result.max_flow
            assert result.shortfall > 0
            # 诊断内部自洽：受限旧格的所有相邻片区都在可达片区内
            for cid in result.restricted_old_ids:
                assert set(adjacency[cid]).issubset(
                    set(result.reachable_district_ids)
                )
