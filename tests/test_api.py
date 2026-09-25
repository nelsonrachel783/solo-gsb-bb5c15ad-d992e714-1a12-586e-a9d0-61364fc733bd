"""API 端到端测试：每个用例使用独立的临时 SQLite 数据库。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


def plan(client, a, b, sample, numerator, denominator, cap):
    return client.post("/pool-plans", json={
        "ingredient_a_batch_id": a,
        "ingredient_b_batch_id": b,
        "sample_id": sample,
        "numerator": numerator,
        "denominator": denominator,
        "max_total_portions": cap,
    })


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_sample(client, name):
    r = client.post("/samples", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_pool(client, name, recipe):
    r = client.post("/pools", json={"name": name, "recipe": recipe})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def fractions_of(client, batch_id):
    r = client.get(f"/batches/{batch_id}/composition")
    assert r.status_code == 200, r.text
    return {
        c["sample_id"]: (c["numerator"], c["denominator"])
        for c in r.json()["components"]
    }


def test_single_sample_composition(client):
    s1 = make_sample(client, "S1")
    assert fractions_of(client, s1) == {s1: (1, 1)}


def test_pool_composition_is_reduced(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 2:2 投入，结果应约分为 1/2 + 1/2
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 2},
    ])
    assert fractions_of(client, p1) == {s1: (1, 2), s2: (1, 2)}


def test_multilevel_same_source_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    # P2 = 1×P1 + 1×S1：S1 经两条路径到达，须合并为 1/2·1/2 + 1/2 = 3/4
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    assert fractions_of(client, p2) == {s1: (3, 4), s2: (1, 4)}


def test_deep_chain(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # P3 = 3×P2 + 1×S2 → S1 = 3/4·3/4 = 9/16，S2 = 3/4·1/4 + 1/4 = 7/16
    p3 = make_pool(client, "P3", [
        {"batch_id": p2, "portions": 3},
        {"batch_id": s2, "portions": 1},
    ])
    assert fractions_of(client, p3) == {s1: (9, 16), s2: (7, 16)}


def test_duplicate_recipe_entries_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ]})
    assert r.status_code == 201
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]
    assert fractions_of(client, body["id"]) == {s1: (3, 4), s2: (1, 4)}


def test_empty_recipe_rejected(client):
    r = client.post("/pools", json={"name": "P1", "recipe": []})
    assert r.status_code == 422


def test_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": 999, "portions": 1},
    ]})
    assert r.status_code == 400
    assert "999" in r.json()["detail"]


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_non_positive_or_non_integer_portions_rejected(client, portions):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": portions},
    ]})
    assert r.status_code == 422


def test_query_unknown_batch_returns_404(client):
    assert client.get("/batches/4242/composition").status_code == 404
    assert client.get("/batches/4242").status_code == 404


def test_duplicate_name_rejected(client):
    make_sample(client, "S1")
    r = client.post("/samples", json={"name": "S1"})
    assert r.status_code == 409


def test_pool_can_only_reference_existing_batches(client):
    # 引用尚未创建的批次（未来的 id）同样被拒绝
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1 + 100, "portions": 1},
    ]})
    assert r.status_code == 400


def preview(client, batch_id, recipe):
    return client.post(f"/batches/{batch_id}/recipe-preview", json={"recipe": recipe})


def preview_components(batch):
    """{(sample_id): ((原分子, 原分母), (预演分子, 预演分母), (差值分子, 差值分母))}"""
    return {
        c["sample_id"]: (
            (c["original"]["numerator"], c["original"]["denominator"]),
            (c["preview"]["numerator"], c["preview"]["denominator"]),
            (c["delta"]["numerator"], c["delta"]["denominator"]),
        )
        for c in batch["components"]
    }


def test_recipe_preview_basic(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # 把 P1 的配方预演替换为 3×S1 + 1×S2
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 3},
        {"batch_id": s2, "portions": 1},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == p1
    assert body["batch_name"] == "P1"
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]
    # 受影响批次 = 目标自身 + 下游 P2，按 id 升序、各出现一次
    assert [b["batch_id"] for b in body["affected_batches"]] == [p1, p2]
    b1, b2 = body["affected_batches"]
    assert preview_components(b1) == {
        s1: ((1, 2), (3, 4), (1, 4)),
        s2: ((1, 2), (1, 4), (-1, 4)),
    }
    # P2' = 1/2·P1' + 1/2·S1 → S1 = 7/8，S2 = 1/8
    assert preview_components(b2) == {
        s1: ((3, 4), (7, 8), (1, 8)),
        s2: ((1, 4), (1, 8), (-1, 8)),
    }


def test_recipe_preview_merges_duplicate_ingredients(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]


def test_recipe_preview_multi_path_merged_and_deduped(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # P3 经 P1、P2 两条路径汇合到 P1；与 P1 无关的 P4 不应受影响
    p3 = make_pool(client, "P3", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": p2, "portions": 1},
    ])
    s3 = make_sample(client, "S3")
    p4 = make_pool(client, "P4", [{"batch_id": s3, "portions": 1}])

    # 把 P1 预演替换为纯 S2
    r = preview(client, p1, [{"batch_id": s2, "portions": 1}])
    assert r.status_code == 200, r.text
    affected = {b["batch_id"]: b for b in r.json()["affected_batches"]}
    assert set(affected) == {p1, p2, p3}  # 多路径汇合，同一批次只返回一次

    # P1' = S2；P2' = 1/2·P1' + 1/2·S1；P3' = 1/2·P1' + 1/2·P2'
    assert preview_components(affected[p1]) == {
        s1: ((1, 2), (0, 1), (-1, 2)),
        s2: ((1, 2), (1, 1), (1, 2)),
    }
    assert preview_components(affected[p2]) == {
        s1: ((3, 4), (1, 2), (-1, 4)),
        s2: ((1, 4), (1, 2), (1, 4)),
    }
    assert preview_components(affected[p3]) == {
        s1: ((5, 8), (1, 4), (-3, 8)),
        s2: ((3, 8), (3, 4), (3, 8)),
    }


def test_recipe_preview_does_not_modify_database(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    before_detail = client.get(f"/batches/{p1}").json()
    before_list = client.get("/batches").json()
    before_comp_p1 = fractions_of(client, p1)
    before_comp_p2 = fractions_of(client, p2)

    r = preview(client, p1, [{"batch_id": s2, "portions": 5}])
    assert r.status_code == 200, r.text

    # 预演后详情、列表与占比查询结果保持原样
    assert client.get(f"/batches/{p1}").json() == before_detail
    assert client.get("/batches").json() == before_list
    assert fractions_of(client, p1) == before_comp_p1
    assert fractions_of(client, p2) == before_comp_p2


def test_recipe_preview_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 1},
        {"batch_id": 999, "portions": 1},
    ])
    assert r.status_code == 400
    assert "999" in r.json()["detail"]


def test_recipe_preview_ingredient_must_predate_target(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    s2 = make_sample(client, "S2")  # 比 P1 更晚创建，不能作为 P1 的预演原料
    r = preview(client, p1, [{"batch_id": s2, "portions": 1}])
    assert r.status_code == 400
    assert str(s2) in r.json()["detail"]
    # 自引用同样会形成环，被拒绝
    r = preview(client, p1, [{"batch_id": p1, "portions": 1}])
    assert r.status_code == 400


def test_recipe_preview_sample_target_rejected(client):
    s1 = make_sample(client, "S1")
    r = preview(client, s1, [{"batch_id": s1, "portions": 1}])
    assert r.status_code == 400


def test_recipe_preview_unknown_target_returns_404(client):
    assert preview(client, 4242, [{"batch_id": 1, "portions": 1}]).status_code == 404


def test_recipe_preview_empty_recipe_rejected(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    assert preview(client, p1, []).status_code == 422


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_recipe_preview_non_positive_or_non_integer_portions_rejected(client, portions):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    assert preview(client, p1, [{"batch_id": s1, "portions": portions}]).status_code == 422


# --------------------------------------------------------------------------- #
# POST /pool-plans
# --------------------------------------------------------------------------- #


def test_pool_plan_pure_samples_half_is_one_to_one(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, 1, 2, 10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_id"] == s1
    assert body["target"] == {"numerator": 1, "denominator": 2}
    assert body["total_portions"] == 2
    assert body["recipe"] == [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ]
    assert body["components"] == [
        {"sample_id": s1, "sample_name": "S1", "numerator": 1, "denominator": 2},
        {"sample_id": s2, "sample_name": "S2", "numerator": 1, "denominator": 2},
    ]


def test_pool_plan_returns_minimal_integer_solution(client):
    # 纯原料 a=1、b=0，目标 2/3 → x:y = 2:1（不是 4:2 等更大解）
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, 2, 3, 100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ]
    assert body["total_portions"] == 3
    assert body["target"] == {"numerator": 2, "denominator": 3}


def test_pool_plan_target_fraction_is_reduced(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 4/6 = 2/3，target 返回约分形式
    r = plan(client, s1, s2, s1, 4, 6, 100)
    assert r.status_code == 200, r.text
    assert r.json()["target"] == {"numerator": 2, "denominator": 3}


def test_pool_plan_multilevel_ingredients(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # P1 = S1/S2 → S1=1/2；P2 = P1/S3 → S1=1/4
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s3, "portions": 1}])
    # 目标 S1=1/3 落在 (1/4, 1/2) 之间 → x:y = (1/3-1/4):(1/2-1/3) = 1:2
    r = plan(client, p1, p2, s1, 1, 3, 100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": p1, "portions": 1},
        {"batch_id": p2, "portions": 2},
    ]
    assert body["total_portions"] == 3
    assert {(c["sample_id"], c["numerator"], c["denominator"]) for c in body["components"]} == {
        (s1, 1, 3), (s2, 1, 3), (s3, 1, 3),
    }


def test_pool_plan_recipe_order_matches_request(client):
    # 交换两个原料的位置，份数也要对应交换
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r1 = plan(client, s1, s2, s1, 2, 3, 100).json()
    r2 = plan(client, s2, s1, s1, 2, 3, 100).json()
    assert r1["recipe"] == [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}]
    assert r2["recipe"] == [
        {"batch_id": s2, "portions": 1}, {"batch_id": s1, "portions": 2}]
    assert r1["components"] == r2["components"]


def test_pool_plan_equal_ingredient_ratios_uses_one_to_one(client):
    # Q = S1/S2、R = S2/S3：S2 在两原料中都是 1/2，目标 1/2 时取 1:1
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    q = make_pool(client, "Q", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    rpool = make_pool(client, "R", [
        {"batch_id": s2, "portions": 1}, {"batch_id": s3, "portions": 1}])
    r = plan(client, q, rpool, s2, 1, 2, 100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": q, "portions": 1}, {"batch_id": rpool, "portions": 1}]
    assert body["total_portions"] == 2
    # 目标样本确实达到 1/2
    s2_comp = next(c for c in body["components"] if c["sample_id"] == s2)
    assert (s2_comp["numerator"], s2_comp["denominator"]) == (1, 2)


def test_pool_plan_recipe_is_submittable_to_pools(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    planned = plan(client, p1, s1, s1, 3, 4, 100).json()
    # 配方可直接提交 /pools（无多余字段），创建后实际占比与规划一致
    r = client.post("/pools", json={"name": "PNEW", "recipe": planned["recipe"]})
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    assert fractions_of(client, new_id) == {
        c["sample_id"]: (c["numerator"], c["denominator"])
        for c in planned["components"]
    }


def test_pool_plan_does_not_create_batches(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    before = client.get("/batches").json()
    assert plan(client, s1, s2, s1, 1, 2, 10).status_code == 200
    # 目标不可达（9/10 最小解总份数 10，上限 9）的规划也不应写库
    assert plan(client, s1, s2, s1, 9, 10, 9).status_code == 422
    assert client.get("/batches").json() == before


def test_pool_plan_target_outside_range_returns_422(client):
    # a=b=1/2 而目标 3/4，正份数加权平均不可能达到
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    q = make_pool(client, "Q", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    rpool = make_pool(client, "R", [
        {"batch_id": s2, "portions": 1}, {"batch_id": s3, "portions": 1}])
    r = plan(client, q, rpool, s2, 3, 4, 100)
    assert r.status_code == 422
    assert "无法用正整数份数达到" in r.json()["detail"]


def test_pool_plan_target_equal_single_endpoint_returns_422(client):
    # a=1/2、b=0，目标 1/2 只等于一端：另一原料份数必为 0
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, 1, 2, 100)
    assert r.status_code == 200  # 对照组：目标严格在区间内
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = plan(client, p1, s2, s1, 1, 2, 100)
    assert r.status_code == 422
    assert "无法用正整数份数达到" in r.json()["detail"]


def test_pool_plan_zero_target_against_pure_sample_returns_422(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 目标 0，而原料 a 本身就是该样本（占比 1/1）→ 必须 0 份 a
    r = plan(client, s1, s2, s1, 0, 1, 100)
    assert r.status_code == 422
    assert "无法用正整数份数达到" in r.json()["detail"]


def test_pool_plan_minimal_solution_over_cap_returns_422(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 最小解总份数 3，上限 2
    r = plan(client, s1, s2, s1, 2, 3, 2)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "3" in detail and "2" in detail and "上限" in detail
    # 恰好等于上限时接受
    assert plan(client, s1, s2, s1, 2, 3, 3).status_code == 200


@pytest.mark.parametrize("numerator,denominator", [
    (1, 0),     # 分母为 0
    (-1, 2),    # 负分子
    (3, 2),     # 分子 > 分母
    (1.5, 2),   # 小数
    ("1", 2),   # 字符串
    (True, 2),  # 布尔
])
def test_pool_plan_invalid_target_rejected(client, numerator, denominator):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, numerator, denominator, 10)
    assert r.status_code == 422


@pytest.mark.parametrize("cap", [0, -1, 1.5, 2.0, "3", True])
def test_pool_plan_invalid_cap_rejected(client, cap):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = client.post("/pool-plans", json={
        "ingredient_a_batch_id": s1,
        "ingredient_b_batch_id": s2,
        "sample_id": s1,
        "numerator": 1,
        "denominator": 2,
        "max_total_portions": cap,
    })
    assert r.status_code == 422


def test_pool_plan_same_ingredient_rejected(client):
    s1 = make_sample(client, "S1")
    r = plan(client, s1, s1, s1, 1, 2, 10)
    assert r.status_code == 422
    assert "不同" in r.json()["detail"][0]["msg"]


def test_pool_plan_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 未知原料
    r = plan(client, 999, s2, s1, 1, 2, 10)
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    # 未知目标样本
    r = plan(client, s1, s2, 998, 1, 2, 10)
    assert r.status_code == 400
    assert "998" in r.json()["detail"]


def test_pool_plan_sample_id_must_be_original_sample(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = plan(client, s1, p1, p1, 1, 2, 10)
    assert r.status_code == 400
    assert "原始样本" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# 原始样本禁配规则：POST/GET /exclusion-rules 与 /pools 冲突检测
# --------------------------------------------------------------------------- #


def add_rule(client, a, b):
    return client.post("/exclusion-rules", json={"sample_a_id": a, "sample_b_id": b})


def test_exclusion_rule_register_and_list(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = add_rule(client, s1, s2)
    assert r.status_code == 201, r.text
    body = r.json()
    # 无序对按 id 升序存储与返回
    assert body["sample_a_id"] == s1
    assert body["sample_b_id"] == s2
    assert body["sample_a_name"] == "S1"
    assert body["sample_b_name"] == "S2"
    assert body["id"] >= 1

    rules = client.get("/exclusion-rules").json()
    assert rules == [body]


def test_exclusion_rule_reversed_order_is_same_pair(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    first = add_rule(client, s1, s2)
    assert first.status_code == 201
    rule_id = first.json()["id"]

    # 逆序重复登记：返回已存在的同一条规则，200 且不生成第二条
    r = add_rule(client, s2, s1)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == rule_id
    assert r.json()["sample_a_id"] == s1
    assert r.json()["sample_b_id"] == s2

    # 同序重复同样不生成第二条
    r = add_rule(client, s1, s2)
    assert r.status_code == 200
    assert r.json()["id"] == rule_id
    assert len(client.get("/exclusion-rules").json()) == 1


def test_exclusion_rules_listed_in_pair_lexicographic_order(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    s4 = make_sample(client, "S4")
    # 乱序登记 (1,4)、(1,2)、(2,3)
    add_rule(client, s1, s4)
    add_rule(client, s1, s2)
    add_rule(client, s2, s3)
    pairs = [(r["sample_a_id"], r["sample_b_id"])
             for r in client.get("/exclusion-rules").json()]
    assert pairs == [(s1, s2), (s1, s4), (s2, s3)]


def test_exclusion_rule_same_sample_rejected(client):
    s1 = make_sample(client, "S1")
    r = add_rule(client, s1, s1)
    assert r.status_code == 422
    assert "不同" in r.json()["detail"][0]["msg"]
    assert client.get("/exclusion-rules").json() == []


def test_exclusion_rule_unknown_or_non_sample_rejected(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])

    r = add_rule(client, s1, 999)
    assert r.status_code == 400
    assert "999" in r.json()["detail"]

    r = add_rule(client, s1, p1)
    assert r.status_code == 400
    assert "原始样本" in r.json()["detail"]

    assert client.get("/exclusion-rules").json() == []


def test_pool_direct_exclusion_conflict_returns_409(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    add_rule(client, s1, s3)

    r = client.post("/pools", json={"name": "BAD", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s3, "portions": 2},
    ]})
    assert r.status_code == 409
    # 冲突样本对按 id 升序返回
    detail = r.json()["detail"]
    assert detail["conflicting_sample_ids"] == [s1, s3]

    # 不创建批次或配方：没有名为 BAD 的批次，批次总数仍为 3
    names = {b["name"] for b in client.get("/batches").json()}
    assert "BAD" not in names
    assert len(names) == 3
    # 冲突导致的名字未被占用，仍可创建无冲突的同名批次
    r = client.post("/pools", json={"name": "BAD", "recipe": [
        {"batch_id": s2, "portions": 1}]})
    assert r.status_code == 201


def test_pool_nested_and_confluence_exclusion_conflict(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    s4 = make_sample(client, "S4")
    s5 = make_sample(client, "S5")
    # P1 = S1+S2、P2 = S3+S4，各自不含禁配对
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": s3, "portions": 1}, {"batch_id": s4, "portions": 1}])

    add_rule(client, s1, s3)

    # 嵌套汇合：新混样沿配方图可达 {S1,S2,S3,S4}，命中 (S1,S3)
    r = client.post("/pools", json={"name": "BAD", "recipe": [
        {"batch_id": p1, "portions": 1}, {"batch_id": p2, "portions": 1}]})
    assert r.status_code == 409
    assert r.json()["detail"]["conflicting_sample_ids"] == [s1, s3]

    # 更深嵌套：P3 = P1+S5（{1,2,5} 不命中）成功；P4 = P3+P2 命中
    p3 = make_pool(client, "P3", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s5, "portions": 1}])
    r = client.post("/pools", json={"name": "BAD2", "recipe": [
        {"batch_id": p3, "portions": 1}, {"batch_id": p2, "portions": 1}]})
    assert r.status_code == 409
    assert r.json()["detail"]["conflicting_sample_ids"] == [s1, s3]


def test_pool_conflict_returns_lexicographically_first_pair(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    s4 = make_sample(client, "S4")
    # 同时命中 (2,4) 与 (1,3)：字典序第一对是 (1,3)
    add_rule(client, s2, s4)
    add_rule(client, s1, s3)
    r = client.post("/pools", json={"name": "BAD", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
        {"batch_id": s3, "portions": 1},
        {"batch_id": s4, "portions": 1},
    ]})
    assert r.status_code == 409
    assert r.json()["detail"]["conflicting_sample_ids"] == [s1, s3]

    # 首元素相同的两对 (1,4)、(1,2)：取第二个 id 更小的 (1,2)
    add_rule(client, s1, s4)
    add_rule(client, s1, s2)
    r = client.post("/pools", json={"name": "BAD2", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
        {"batch_id": s4, "portions": 1},
    ]})
    assert r.status_code == 409
    assert r.json()["detail"]["conflicting_sample_ids"] == [s1, s2]


def test_exclusion_rules_only_constrain_future_pools(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # 规则登记前已存在的批次 P1 = S1+S2
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    before = fractions_of(client, p1)

    add_rule(client, s1, s2)

    # 已有批次不受影响，详情与占比接口照常
    assert fractions_of(client, p1) == before
    assert client.get(f"/batches/{p1}").status_code == 200

    # 但随后创建的批次即使不直接混合 S1、S2，只要沿旧配方可达该禁配对就拒绝：
    # 新混样 = S3 + P1，可达 {S1,S2,S3}，命中 (S1,S2)
    r = client.post("/pools", json={"name": "BAD", "recipe": [
        {"batch_id": p1, "portions": 1}, {"batch_id": s3, "portions": 1}]})
    assert r.status_code == 409
    assert r.json()["detail"]["conflicting_sample_ids"] == [s1, s2]

    # 不含该对的新批次仍可创建
    assert client.post("/pools", json={"name": "OK", "recipe": [
        {"batch_id": s3, "portions": 1}]}).status_code == 201
