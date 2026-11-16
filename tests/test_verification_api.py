"""核定供数 API 集成测试。

覆盖：
- 相容提交保存逐格整数流向、两侧合计吻合、与当前发布任务绑定；
- 总量相同却不可行：422 + 受限旧格/可达片区/缺额，失败留痕且不覆盖旧核定；
- 编号缺失/多余/总量不符：400 且不留记录；
- 发布交错：生成任务切换后旧核定不被当成新结果，新任务重新核定后才有效；
- 输入校验、无发布任务 409、/result 行为不变、失败与成功记录均可追溯。
"""
from __future__ import annotations

from tests.conftest import poll_generation

# 两个上下拼接的旧格，各 10x10；g1 在上半，g2 在下半
OLD_V1 = {
    "items": [
        {"id": "g1", "rect": [0, 0, 10, 10], "households": 6},
        {"id": "g2", "rect": [0, 10, 10, 20], "households": 4},
    ]
}
# d1/d2 各占左右半条、拼合完整覆盖旧网格；g1、g2 都同时与两区相交。
NEW_V1 = {
    "items": [
        {"id": "d1", "rect": [0, 0, 5, 20]},
        {"id": "d2", "rect": [5, 0, 10, 20]},
    ]
}

# 局部冲突几何（仍满足生成的完整覆盖要求）：
# d1 为整条左半（与 g1、g2 都相交）；d3 只补上半右侧（仅与 g1 相交）；
# d2 只压下半右侧（仅与 g2 相交）。g1 的邻接片区是 d1/d3，g2 的邻接片区是 d1/d2。
NEW_TRAPPED = {
    "items": [
        {"id": "d1", "rect": [0, 0, 5, 20]},
        {"id": "d3", "rect": [5, 0, 10, 10]},
        {"id": "d2", "rect": [5, 10, 10, 20]},
    ]
}


def _publish(client, old=OLD_V1, new=NEW_V1):
    r_old = client.post("/old-grids", json=old)
    assert r_old.status_code == 201, r_old.text
    r_new = client.post("/new-districts", json=new)
    assert r_new.status_code == 201, r_new.text
    gen = poll_generation(client, client.post("/generations").json()["id"])
    assert gen["status"] == "succeeded", gen
    return gen


def _verif(client, items, expected):
    r = client.post("/verifications", json={"items": items})
    assert r.status_code == expected, r.text
    return r.json()


def test_verification_requires_published_result(client):
    # 没有任何已发布任务 -> 409
    r = client.post(
        "/verifications", json={"items": [{"id": "d1", "households": 1}]}
    )
    assert r.status_code == 409
    assert client.get("/verifications/current").status_code == 404


def test_feasible_verification_persists_integer_flows_and_totals(client):
    gen = _publish(client)

    # 几何上 g2 与 d1/d2 等面积相交，但核定允许自由整数拆分（不按面积比例）：
    # g1=6 拆 5/1，g2=4 拆 2/2 -> d1=7、d2=3
    body = _verif(
        client,
        [{"id": "d1", "households": 7}, {"id": "d2", "households": 3}],
        201,
    )
    assert body["status"] == "feasible"
    assert body["generation_id"] == gen["id"]
    assert body["old_version"] == gen["old_version"]
    assert body["new_version"] == gen["new_version"]
    assert body["total_households"] == 10
    assert body["shortfall"] is None

    # 具体逐边拆分由最大流增广顺序决定，这里只断言结构与双侧守恒
    flow_map = {(f["old_id"], f["district_id"]): f["households"] for f in body["flows"]}
    # 所有边都来自真实相交关系
    assert set(flow_map) <= {
        ("g1", "d1"), ("g1", "d2"), ("g2", "d1"), ("g2", "d2")
    }
    # 逐旧格合计等于原户数
    assert sum(v for (g, _), v in flow_map.items() if g == "g1") == 6
    assert sum(v for (g, _), v in flow_map.items() if g == "g2") == 4
    # 旧格侧合计等于原户数，片区侧合计等于核定
    old_totals = {t["old_id"]: t["households"] for t in body["old_totals"]}
    assert old_totals == {"g1": 6, "g2": 4}
    district_totals = {t["district_id"]: t["households"] for t in body["district_totals"]}
    assert district_totals == {"d1": 7, "d2": 3}
    assert sum(f["households"] for f in body["flows"]) == 10

    # 当前有效核定可查，且与提交记录一致
    current = client.get("/verifications/current")
    assert current.status_code == 200
    assert current.json()["id"] == body["id"]
    assert current.json()["flows"] == body["flows"]

    # 列表与单条查询
    listing = client.get("/verifications").json()
    assert [v["id"] for v in listing] == [body["id"]]
    assert client.get(f"/verifications/{body['id']}").json()["status"] == "feasible"
    assert client.get("/verifications/999").status_code == 404


def test_infeasible_same_total_returns_diagnosis_and_keeps_old_verification(client):
    # 先用普通几何建立一份有效核定
    _publish(client)
    good = _verif(
        client,
        [{"id": "d1", "households": 7}, {"id": "d2", "households": 3}],
        201,
    )

    # 再发布带局部冲突的三片区版本：g1 只能把户数送进 d1/d3
    client.post("/old-grids", json=OLD_V1)
    client.post("/new-districts", json=NEW_TRAPPED)
    gen2 = poll_generation(client, client.post("/generations").json()["id"])
    assert gen2["status"] == "succeeded"
    # 新发布切换后，上一份核定已不再是当前有效核定
    assert client.get("/verifications/current").status_code == 404

    # 总量仍为 10，但 g1 的邻接片区 d1+d3 只核定 3（g1 有 6 户），缺额 3 -> 不可行
    r = client.post(
        "/verifications",
        json={
            "items": [
                {"id": "d1", "households": 2},
                {"id": "d2", "households": 7},
                {"id": "d3", "households": 1},
            ]
        },
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["shortfall"] == 3
    assert detail["blocked_old_ids"] == ["g1"]
    assert detail["reachable_district_ids"] == ["d1", "d3"]
    assert detail["generation_id"] == gen2["id"]
    failed_id = detail["verification_id"]

    # 失败记录留痕，状态为 infeasible，无流向
    failed = client.get(f"/verifications/{failed_id}").json()
    assert failed["status"] == "infeasible"
    assert failed["flows"] == []
    assert failed["shortfall"] == 3
    assert failed["blocked_old_ids"] == ["g1"]
    assert failed["reachable_district_ids"] == ["d1", "d3"]

    # 仍然没有当前有效核定（失败不覆盖，且旧核定属于已切换前的任务）
    assert client.get("/verifications/current").status_code == 404

    # 提交一份相容核定后恢复有效；失败记录仍可在列表中追溯
    ok = _verif(
        client,
        [
            {"id": "d1", "households": 4},
            {"id": "d2", "households": 2},
            {"id": "d3", "households": 4},
        ],
        201,
    )
    current = client.get("/verifications/current").json()
    assert current["id"] == ok["id"]
    listing = client.get("/verifications").json()
    statuses = {v["id"]: v["status"] for v in listing}
    assert statuses[failed_id] == "infeasible"
    assert statuses[good["id"]] == "feasible"
    assert statuses[ok["id"]] == "feasible"


def test_mismatched_ids_and_total_are_rejected_without_trace(client):
    _publish(client)

    # 缺少 d2 -> 400
    r = client.post("/verifications", json={"items": [{"id": "d1", "households": 10}]})
    assert r.status_code == 400 and "缺少" in r.json()["detail"]

    # 出现未知片区 -> 400
    r = client.post(
        "/verifications",
        json={
            "items": [
                {"id": "d1", "households": 5},
                {"id": "dX", "households": 5},
            ]
        },
    )
    assert r.status_code == 400 and "未知" in r.json()["detail"]

    # 编号一致但总量不符 -> 400，信息里给出两侧总量
    r = client.post(
        "/verifications",
        json={"items": [{"id": "d1", "households": 9}, {"id": "d2", "households": 2}]},
    )
    assert r.status_code == 400
    assert "11" in r.json()["detail"] and "10" in r.json()["detail"]

    # 被拒提交不留任何记录，也没有有效核定
    assert client.get("/verifications").json() == []
    assert client.get("/verifications/current").status_code == 404


def test_pydantic_level_validation(client):
    _publish(client)
    # 负户数 -> 422（Pydantic）
    neg = {"items": [{"id": "d1", "households": -1}, {"id": "d2", "households": 11}]}
    assert client.post("/verifications", json=neg).status_code == 422
    # 空提交 -> 422
    assert client.post("/verifications", json={"items": []}).status_code == 422
    # 编号重复 -> 422
    dup = {
        "items": [
            {"id": "d1", "households": 5},
            {"id": "d1", "households": 5},
        ]
    }
    assert client.post("/verifications", json=dup).status_code == 422
    # 超过 12 条 -> 422
    too_many = {"items": [{"id": f"d{i}", "households": 1} for i in range(13)]}
    assert client.post("/verifications", json=too_many).status_code == 422
    # 全部被 Pydantic 拦截，无落库
    assert client.get("/verifications").json() == []


def test_republish_switch_invalidates_old_verification(client):
    """发布交错：生成任务切换后，旧核定不再是当前有效核定。"""
    gen1 = _publish(client)
    v1 = _verif(
        client,
        [{"id": "d1", "households": 7}, {"id": "d2", "households": 3}],
        201,
    )
    assert v1["generation_id"] == gen1["id"]
    assert client.get("/verifications/current").json()["id"] == v1["id"]

    # 替换输入并重新生成发布：新任务只有一个片区 e1、一个旧格 h1（户数 5）
    client.post(
        "/old-grids",
        json={"items": [{"id": "h1", "rect": [0, 0, 10, 10], "households": 5}]},
    )
    client.post(
        "/new-districts",
        json={"items": [{"id": "e1", "rect": [0, 0, 10, 10]}]},
    )
    gen2 = poll_generation(client, client.post("/generations").json()["id"])
    assert gen2["status"] == "succeeded"
    assert gen2["id"] != gen1["id"]

    # 旧核定仍可按编号追溯，但 /current 不再返回它（不能当成新任务的结果）
    assert client.get(f"/verifications/{v1['id']}").json()["id"] == v1["id"]
    assert client.get("/verifications/current").status_code == 404

    # 拿旧片区编号去新任务核定 -> 400（编号不符），不产生新记录、不改变现状
    r = client.post(
        "/verifications", json={"items": [{"id": "d1", "households": 5}]}
    )
    assert r.status_code == 400 and "未知" in r.json()["detail"]
    assert client.get("/verifications/current").status_code == 404

    # 按新任务编号重新核定成功后，当前有效核定指向新任务
    v2 = _verif(client, [{"id": "e1", "households": 5}], 201)
    assert v2["generation_id"] == gen2["id"]
    assert v2["old_version"] == gen2["old_version"]
    current = client.get("/verifications/current").json()
    assert current["id"] == v2["id"]
    assert current["generation_id"] == gen2["id"]
    assert {(f["old_id"], f["district_id"], f["households"]) for f in current["flows"]} == {
        ("h1", "e1", 5)
    }


def test_verification_uses_geometry_not_area_ratio_and_result_unchanged(client):
    # 几何上 g1/g2 都与 d1/d2 等面积相交，但核定允许把 g1 的 6 户全部送 d1、
    # g2 的 4 户全部送 d2，完全不按 50/50 的面积比例。
    _publish(client)
    body = _verif(
        client,
        [{"id": "d1", "households": 6}, {"id": "d2", "households": 4}],
        201,
    )
    flow_map = {(f["old_id"], f["district_id"]): f["households"] for f in body["flows"]}
    # 存在一种完全不按面积比例的整数解：g1 全给 d1、g2 全给 d2
    assert flow_map == {("g1", "d1"): 6, ("g2", "d2"): 4}

    # 核定供数不改变 /result 的原有行为与内容
    result = client.get("/result").json()
    assert result["total_households"] == 10
    by_district = {d["district_id"]: d for d in result["districts"]}
    # 原有最大余数分摊结果不受核定影响：
    # g1=6 在等面积 d1/d2 上整除为 3/3；g2=4 为 2/2
    d1_parts = {x["old_id"]: x["households"] for x in by_district["d1"]["detail"]}
    assert d1_parts == {"g1": 3, "g2": 2}
    d2_parts = {x["old_id"]: x["households"] for x in by_district["d2"]["detail"]}
    assert d2_parts == {"g1": 3, "g2": 2}


def test_zero_demand_district_and_zero_household_cell(client):
    # g2 户数为 0；d2 核定 0 是允许的非负整数
    client.post(
        "/old-grids",
        json={
            "items": [
                {"id": "g1", "rect": [0, 0, 10, 10], "households": 6},
                {"id": "g2", "rect": [0, 10, 10, 20], "households": 0},
            ]
        },
    )
    client.post("/new-districts", json=NEW_V1)
    gen = poll_generation(client, client.post("/generations").json()["id"])
    assert gen["status"] == "succeeded"

    body = _verif(
        client,
        [{"id": "d1", "households": 6}, {"id": "d2", "households": 0}],
        201,
    )
    # 零需求片区没有正流量边；零户数旧格同样不出现
    assert all(f["households"] > 0 for f in body["flows"])
    assert {(f["old_id"], f["district_id"], f["households"]) for f in body["flows"]} == {
        ("g1", "d1", 6)
    }
    district_totals = {t["district_id"]: t["households"] for t in body["district_totals"]}
    assert district_totals == {"d1": 6, "d2": 0}


def test_failed_verification_then_success_switches_current(client):
    # 用三片区几何制造一个总量相同但不可行的提交
    client.post("/old-grids", json=OLD_V1)
    client.post("/new-districts", json=NEW_TRAPPED)
    gen = poll_generation(client, client.post("/generations").json()["id"])
    assert gen["status"] == "succeeded"

    r = client.post(
        "/verifications",
        json={
            "items": [
                {"id": "d1", "households": 2},
                {"id": "d2", "households": 7},
                {"id": "d3", "households": 1},
            ]
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["blocked_old_ids"] == ["g1"]
    assert client.get("/verifications/current").status_code == 404

    # 再提交相容版本 -> 成功并成为当前有效核定
    ok = _verif(
        client,
        [
            {"id": "d1", "households": 4},
            {"id": "d2", "households": 2},
            {"id": "d3", "households": 4},
        ],
        201,
    )
    current = client.get("/verifications/current").json()
    assert current["id"] == ok["id"]
