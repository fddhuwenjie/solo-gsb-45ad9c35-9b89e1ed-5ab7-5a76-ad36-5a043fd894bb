import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with TestClient(app) as c:
        yield c


def seed(client, courses, requirements, rules):
    for cid, credits, score in courses:
        assert client.post(
            "/courses", json={"id": cid, "credits": credits, "score": score}
        ).status_code == 201
    for rid, credits in requirements:
        assert client.post(
            "/requirements", json={"id": rid, "required_credits": credits}
        ).status_code == 201
    for cid, rid, min_score in rules:
        assert client.post(
            "/substitutions",
            json={"course_id": cid, "requirement_id": rid, "min_score": min_score},
        ).status_code == 201


def base_seed(client):
    seed(
        client,
        courses=[("C1", 4, 90), ("C2", 6, 75), ("C3", 2, 95)],
        requirements=[("R1", 4), ("R2", 6)],
        rules=[("C1", "R1", 60), ("C2", "R2", 70), ("C3", "R1", 60)],
    )


# ------------------------------------------------------------ 登记校验


def test_register_validation(client):
    assert client.post(
        "/courses", json={"id": "X", "credits": 0, "score": 80}
    ).status_code == 422
    assert client.post(
        "/courses", json={"id": "X", "credits": 3, "score": 101}
    ).status_code == 422
    assert client.post(
        "/requirements", json={"id": "X", "required_credits": -1}
    ).status_code == 422


def test_register_duplicates_conflict(client):
    base_seed(client)
    assert client.post(
        "/courses", json={"id": "C1", "credits": 4, "score": 90}
    ).status_code == 409
    assert client.post(
        "/requirements", json={"id": "R1", "required_credits": 4}
    ).status_code == 409
    assert client.post(
        "/substitutions",
        json={"course_id": "C1", "requirement_id": "R1", "min_score": 60},
    ).status_code == 409


# ------------------------------------------------------------ 批量互认


def test_batch_confirm_and_progress(client):
    base_seed(client)
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C2", "requirement_id": "R2"},
        ]},
    )
    assert resp.status_code == 201
    assert all(r["status"] == "confirmed" for r in resp.json()["created"])

    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 10
    assert progress["remaining_credits"] == 0
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["R1"]["status"] == "confirmed"
    assert by_req["R2"]["status"] == "confirmed"


def test_batch_all_or_nothing(client):
    base_seed(client)
    # C3 学分 2 < R1 所需 4 -> 整批失败，C2/R2 也不写入
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C2", "requirement_id": "R2"},
            {"course_id": "C3", "requirement_id": "R1"},
        ]},
    )
    assert resp.status_code == 409
    assert client.get("/recognitions").json() == []
    assert client.get("/progress").json()["earned_credits"] == 0


def test_batch_rejects_score_below_min_and_missing_rule(client):
    seed(
        client,
        courses=[("L", 4, 50), ("M", 4, 90)],
        requirements=[("RQ", 4)],
        rules=[("L", "RQ", 60)],  # M 没有规则
    )
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "L", "requirement_id": "RQ"}]}
    )
    assert resp.status_code == 409
    assert "min_score" in resp.json()["detail"][0]
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "M", "requirement_id": "RQ"}]}
    )
    assert resp.status_code == 409
    assert "no substitution rule" in resp.json()["detail"][0]
    assert client.get("/recognitions").json() == []


def test_batch_rejects_duplicates_within_batch(client):
    base_seed(client)
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C1", "requirement_id": "R2"},
        ]},
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C3", "requirement_id": "R1"},
        ]},
    )
    assert resp.status_code == 409
    assert client.get("/recognitions").json() == []


def test_batch_rejects_conflict_with_existing(client):
    base_seed(client)
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    ).status_code == 201
    # 同一要求被其它课程抢占 -> 409；同一课程复用 -> 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R2"}]}
    )
    assert resp.status_code == 409
    assert len(client.get("/recognitions").json()) == 1


# ------------------------------------------------------------ 更正 -> 待复核 -> 复核/撤销


def test_correction_moves_to_pending_and_keeps_occupation(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    assert client.get("/progress").json()["earned_credits"] == 4

    # 成绩更正到不达标 -> 待复核、停止计分
    resp = client.put("/courses/C1", json={"score": 55})
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == [1]
    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 0
    assert progress["requirements"][0]["status"] == "pending"
    assert client.get("/recognitions", params={"status": "pending"}).json()[0]["id"] == 1

    # 占用保留：其它申请无法抢走该要求或该课程
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R2"}]}
    )
    assert resp.status_code == 409


def test_review_required_to_recount(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    client.put("/courses/C1", json={"score": 55})

    # 复核仍不合格 -> 维持待复核
    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": False, "status": "pending"}
    assert client.get("/progress").json()["earned_credits"] == 0

    # 成绩恢复后不会自动计分，须显式复核
    client.put("/courses/C1", json={"score": 88})
    assert client.get("/recognitions", params={"status": "pending"}).json()[0]["id"] == 1
    assert client.get("/progress").json()["earned_credits"] == 0

    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": True, "status": "confirmed"}
    assert client.get("/progress").json()["earned_credits"] == 4

    # 已认记录不能再复核
    assert client.post("/recognitions/1/review").status_code == 409


def test_revoke_releases_occupation(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    client.put("/courses/C1", json={"score": 40})  # -> pending

    resp = client.delete("/recognitions/1")
    assert resp.status_code == 200
    assert resp.json()["released"] == {"course_id": "C1", "requirement_id": "R1"}
    assert client.get("/recognitions").json() == []
    progress = client.get("/progress").json()
    assert progress["requirements"][0]["status"] == "vacant"

    # 释放后其它课程可以申请该要求
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409  # C3 学分 2 < 4，业务规则拒绝
    seed(client, [("C4", 4, 80)], [], [("C4", "R1", 60)])
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "C4", "requirement_id": "R1"}]}
    ).status_code == 201


def test_correction_of_credits_also_triggers_pending(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    resp = client.put("/courses/C1", json={"credits": 3})  # 学分不再足额
    assert resp.json()["moved_to_pending"] == [1]
    assert client.get("/progress").json()["earned_credits"] == 0


def test_not_found_and_empty_batch(client):
    base_seed(client)
    assert client.put("/courses/NOPE", json={"score": 1}).status_code == 404
    assert client.post("/recognitions/99/review").status_code == 404
    assert client.delete("/recognitions/99").status_code == 404
    assert client.post("/recognitions", json={"items": []}).status_code == 422
    assert client.put("/courses/C1", json={}).status_code == 422


# ------------------------------------------------------------ 整批改派


def reassign_seed(client):
    """rec1: C1->R1，rec2: C2->R2；R3 空缺；C3 未占用且可替代 R1/R3。"""
    seed(
        client,
        courses=[("C1", 6, 90), ("C2", 6, 80), ("C3", 4, 90)],
        requirements=[("R1", 4), ("R2", 6), ("R3", 3)],
        rules=[
            ("C1", "R1", 60), ("C1", "R2", 60), ("C1", "R3", 60),
            ("C2", "R1", 60), ("C2", "R2", 60), ("C2", "R3", 60),
            ("C3", "R1", 60), ("C3", "R3", 60),
        ],
    )
    assert client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C2", "requirement_id": "R2"},
        ]},
    ).status_code == 201


def test_reassign_updates_requirement_and_keeps_id(client):
    reassign_seed(client)
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "requirement_id": "R3"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["reassigned"] == [
        {
            "id": 1,
            "course_id": "C1",
            "from_requirement_id": "R1",
            "to_requirement_id": "R3",
            "status": "confirmed",
        }
    ]
    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "R3"
    assert recs[1]["status"] == "confirmed"
    progress = client.get("/progress").json()
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["R1"]["status"] == "vacant"
    assert by_req["R3"]["status"] == "confirmed"
    assert by_req["R3"]["recognition_id"] == 1
    assert progress["earned_credits"] == 6 + 3


def test_reassign_swap_within_batch(client):
    """批内两条记录互换要求：原占用同时释放，不会因相互占用而失败。"""
    reassign_seed(client)
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "requirement_id": "R2"},
            {"recognition_id": 2, "requirement_id": "R1"},
        ]},
    )
    assert resp.status_code == 200
    swapped = {r["id"]: r for r in resp.json()["reassigned"]}
    assert swapped[1]["from_requirement_id"] == "R1"
    assert swapped[1]["to_requirement_id"] == "R2"
    assert swapped[2]["from_requirement_id"] == "R2"
    assert swapped[2]["to_requirement_id"] == "R1"

    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "R2"
    assert recs[2]["requirement_id"] == "R1"
    progress = client.get("/progress").json()
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["R1"]["course_id"] == "C2"
    assert by_req["R2"]["course_id"] == "C1"
    assert progress["earned_credits"] == 10


def test_reassign_all_or_nothing(client):
    reassign_seed(client)
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "requirement_id": "R3"},
            {"recognition_id": 2, "requirement_id": "NOPE"},
        ]},
    )
    assert resp.status_code == 409
    # 整批不变：rec1 仍在 R1，进度不变
    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "R1"
    assert recs[2]["requirement_id"] == "R2"
    assert client.get("/progress").json()["earned_credits"] == 10


def test_reassign_rejects_duplicates_empty_and_unknown(client):
    reassign_seed(client)
    assert client.post(
        "/recognitions/reassign", json={"items": []}
    ).status_code == 422
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "requirement_id": "R3"},
            {"recognition_id": 1, "requirement_id": "R2"},
        ]},
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "requirement_id": "R3"},
            {"recognition_id": 2, "requirement_id": "R3"},
        ]},
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 99, "requirement_id": "R3"}]},
    )
    assert resp.status_code == 409
    assert "not found" in resp.json()["detail"][0]
    # 均未生效
    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "R1"
    assert recs[2]["requirement_id"] == "R2"


def test_reassign_rejects_pending_source_and_occupied_target(client):
    reassign_seed(client)
    # 目标被批外"已认"记录占用
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "requirement_id": "R2"}]},
    )
    assert resp.status_code == 409
    assert "occupied" in resp.json()["detail"][0]

    # 成绩更正使 rec1 转待复核
    assert client.put("/courses/C1", json={"score": 50}).json()["moved_to_pending"] == [1]
    # 待复核记录不能作为改派来源
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "requirement_id": "R3"}]},
    )
    assert resp.status_code == 409
    assert "pending" in resp.json()["detail"][0]
    # 目标被批外"待复核"记录占用同样拒绝
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 2, "requirement_id": "R1"}]},
    )
    assert resp.status_code == 409
    assert "occupied" in resp.json()["detail"][0]
    # 状态保持不变
    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "R1"
    assert recs[1]["status"] == "pending"
    assert recs[2]["requirement_id"] == "R2"


def test_reassign_validates_rule_score_and_credits(client):
    seed(
        client,
        courses=[("C1", 4, 70)],
        requirements=[("R1", 4), ("R2", 4), ("R3", 5)],
        rules=[("C1", "R1", 60), ("C1", "R3", 80)],  # C1->R2 无规则
    )
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )

    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "requirement_id": "R2"}]},
    )
    assert resp.status_code == 409
    assert "no substitution rule" in resp.json()["detail"][0]

    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "requirement_id": "R3"}]},
    )
    assert resp.status_code == 409
    detail = " ".join(resp.json()["detail"])
    assert "credits" in detail and "min_score" in detail

    rec = client.get("/recognitions").json()[0]
    assert rec["requirement_id"] == "R1"
    assert client.get("/progress").json()["earned_credits"] == 4


def test_operations_after_reassign_use_new_relations(client):
    """后续操作按改派后的关系判断：原要求已释放、新要求被占用。"""
    reassign_seed(client)
    assert client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "requirement_id": "R3"}]},
    ).status_code == 200

    # R3 现被 rec1 占用；R1 已释放
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R3"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 201

    # 更正也按新关系作用：C1 不达标 -> rec1 在其新要求 R3 上转待复核
    moved = client.put("/courses/C1", json={"score": 50}).json()["moved_to_pending"]
    assert moved == [1]
    by_req = {r["requirement_id"]: r for r in client.get("/progress").json()["requirements"]}
    assert by_req["R3"]["status"] == "pending"
    assert by_req["R3"]["recognition_id"] == 1


# ------------------------------------------------------------ 并发


def test_concurrent_batches_no_double_use(client):
    base_seed(client)

    def submit(i):
        with TestClient(app) as c:
            course = "C1" if i % 2 == 0 else "C3"
            return c.post(
                "/recognitions",
                json={"items": [{"course_id": course, "requirement_id": "R1"}]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(submit, range(16)))

    assert codes.count(201) == 1
    assert codes.count(409) == 15
    recs = client.get("/recognitions").json()
    assert len(recs) == 1
    assert client.get("/progress").json()["earned_credits"] == 4


def test_concurrent_correction_and_submit_consistent(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )

    def correct(i):
        with TestClient(app) as c:
            return c.put("/courses/C1", json={"score": 50 if i % 2 else 95}).status_code

    def submit(_):
        with TestClient(app) as c:
            return c.post(
                "/recognitions",
                json={"items": [{"course_id": "C3", "requirement_id": "R1"}]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda i: correct(i) if i % 2 else submit(i), range(20)))

    assert all(code in (200, 409) for code in results)
    # 无论交错顺序如何，R1 始终只有一条占用记录
    recs = client.get("/recognitions").json()
    assert len([r for r in recs if r["requirement_id"] == "R1"]) == 1


def test_concurrent_reassign_and_correction_serialized(client):
    """改派与更正串行：改派成功当且仅当它排在首次更正之前。"""
    reassign_seed(client)

    def reassign(_):
        with TestClient(app) as c:
            return c.post(
                "/recognitions/reassign",
                json={"items": [{"recognition_id": 1, "requirement_id": "R3"}]},
            ).status_code

    def correct(_):
        with TestClient(app) as c:
            return c.put("/courses/C1", json={"score": 50}).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [
            ex.submit(reassign if i % 2 == 0 else correct, i) for i in range(20)
        ]
    reassign_codes = [futures[i].result() for i in range(0, 20, 2)]
    correct_codes = [futures[i].result() for i in range(1, 20, 2)]

    assert all(code == 200 for code in correct_codes)
    assert all(code in (200, 409) for code in reassign_codes)

    recs = client.get("/recognitions").json()
    assert len(recs) == 2
    by_id = {r["id"]: r for r in recs}
    # 更正必然执行过：rec1 转待复核；其要求取决于改派是否抢先成功
    assert by_id[1]["status"] == "pending"
    reassign_succeeded = 200 in reassign_codes
    assert by_id[1]["requirement_id"] == ("R3" if reassign_succeeded else "R1")
    assert by_id[2]["status"] == "confirmed"
    assert client.get("/progress").json()["earned_credits"] == 6


def test_progress_snapshot_consistent_under_concurrent_reassign(client):
    """进度查询取自同一提交快照：并发互换改派时不得读出混合状态。"""
    reassign_seed(client)
    failures = []

    def swap(_):
        with TestClient(app) as c:
            for _ in range(15):
                c.post("/recognitions/reassign", json={"items": [
                    {"recognition_id": 1, "requirement_id": "R2"},
                    {"recognition_id": 2, "requirement_id": "R1"},
                ]})
                c.post("/recognitions/reassign", json={"items": [
                    {"recognition_id": 1, "requirement_id": "R1"},
                    {"recognition_id": 2, "requirement_id": "R2"},
                ]})

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(swap, i) for i in range(2)]
        for _ in range(300):
            progress = client.get("/progress").json()
            by_req = {r["requirement_id"]: r for r in progress["requirements"]}
            r1, r2 = by_req["R1"], by_req["R2"]
            # 任一提交状态下：R1/R2 各被一条已认占用、课程不同、已得学分恒为 10
            if not (
                r1["status"] == "confirmed"
                and r2["status"] == "confirmed"
                and r1["course_id"] != r2["course_id"]
                and progress["earned_credits"] == 10
            ):
                failures.append(progress)
        for f in futs:
            f.result()
    assert not failures


# ------------------------------------------------------------ 持久化

def test_records_survive_restart(client, tmp_path, monkeypatch):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    # 模拟重启：重新初始化（幂等）并新建客户端，同一数据库文件
    db.init_db()
    with TestClient(app) as c2:
        assert len(c2.get("/recognitions").json()) == 1
        assert c2.get("/progress").json()["earned_credits"] == 4
