"""年代证据服务的接口与链路测试。"""
from __future__ import annotations

import sqlite3

import pytest

from app.database import connection as db_connection


def _auth(client, username="chrono_user", password="ChronoPass!23"):
    client.post("/api/users", json={"username": username, "display_name": "年代研究员", "password": password})
    token = client.post("/api/sessions", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _register(client, headers, *, field_code="H3-7", context_type="pit", context_name="H3 灰坑第4层", material="charcoal"):
    r = client.post(
        "/api/chrono/samples",
        headers=headers,
        json={"field_code": field_code, "context_type": context_type, "context_name": context_name, "material": material},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _complete_chain(client, headers, lab_no):
    steps = [
        {"event_type": "seal", "seal_id": f"SEAL-{lab_no}"},
        {"event_type": "handover", "from_party": "发掘队", "to_party": "年代学实验室", "actor": "张三"},
        {"event_type": "receive", "actor": "王五"},
        {"event_type": "open", "actor": "王五"},
    ]
    for step in steps:
        r = client.post(f"/api/chrono/samples/{lab_no}/seal-events", headers=headers, json=step)
        assert r.status_code == 201, r.text
    return r.json()


def _ready_sample(client, headers, **register_kw):
    sample = _register(client, headers, **register_kw)
    _complete_chain(client, headers, sample["lab_no"])
    return sample


def _measure(client, headers, lab_no, age=2300, error=30):
    r = client.post(
        f"/api/chrono/samples/{lab_no}/measurements",
        headers=headers,
        json={"c14_age_bp": age, "c14_error_bp": error, "delta_c13": -25.0, "instrument": "AMS-1", "operator": "钱七"},
    )
    assert r.status_code == 201, r.text
    return r.json()["measurements"][-1]["measurement_no"]


def _calibrate(client, headers, lab_no, measurement_no, *, curve="INTCAL23-MINI", **extra):
    r = client.post(
        f"/api/chrono/samples/{lab_no}/calibrations",
        headers=headers,
        json={"measurement_no": measurement_no, "curve_name": curve, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def headers(client):
    h = _auth(client)
    row = client.get("/api/chrono/curves", headers=h)
    assert row.status_code == 200
    names = {item["name"] for item in row.json()["data"]}
    assert {"INTCAL23-MINI", "MARINE23-MINI"} <= names
    return h


# ----------------------------------------------------------------- 身份与链路
def test_lab_identity_and_source_view(client, headers):
    s1 = _register(client, headers)
    s2 = _register(client, headers, field_code="T2-9", context_type="stratum", context_name="第6层", material="bone")
    assert s1["lab_no"] == "LAB-000001" and s2["lab_no"] == "LAB-000002"
    fetched = client.get(f"/api/chrono/samples/{s1['lab_no']}", headers=headers).json()
    assert fetched["source"] == {"context_type": "pit", "context_name": "H3 灰坑第4层", "material": "charcoal"}
    assert fetched["chain_status"] == "registered"
    listing = client.get("/api/chrono/samples?context_type=stratum", headers=headers).json()["data"]
    assert [item["lab_no"] for item in listing] == ["LAB-000002"]


def test_chain_cannot_skip_handover(client, headers):
    sample = _register(client, headers)
    # 未封签直接交接
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/seal-events", headers=headers,
                    json={"event_type": "handover", "from_party": "a", "to_party": "b"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "chain_transition_denied"
    # 直接开封同样被拒绝
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/seal-events", headers=headers, json={"event_type": "open"})
    assert r.status_code == 409
    # 未开封不能前处理
    client.post(f"/api/chrono/samples/{sample['lab_no']}/seal-events", headers=headers, json={"event_type": "seal", "seal_id": "S1"})
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                    json={"method": "ABA", "operator": "x", "conclusion": "pass"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sample_not_opened"


def test_damaged_seal_quarantines_and_reseal_restores(client, headers):
    sample = _ready_sample(client, headers)
    lab = sample["lab_no"]
    # 重新构造一个破损样品更直观：登记新样并在交接中破损
    damaged = _register(client, headers, field_code="D-1")
    lab_d = damaged["lab_no"]
    client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "seal", "seal_id": "SD"})
    client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "handover", "from_party": "a", "to_party": "b"})
    r = client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "damage", "note": "撕裂"})
    assert r.json()["chain_status"] == "quarantined"
    assert "seal_damaged_unresolved" in r.json()["risk_flags"]
    # 破损后不能直接接收/开封
    assert client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "receive"}).status_code == 409
    # 必须重新封签并重新交接
    client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "reseal", "seal_id": "SD2"})
    assert client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "receive"}).status_code == 409
    client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "handover", "from_party": "a", "to_party": "b"})
    opened = client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "receive"})
    client.post(f"/api/chrono/samples/{lab_d}/seal-events", headers=headers, json={"event_type": "open"})
    view = client.get(f"/api/chrono/samples/{lab_d}", headers=headers).json()
    assert view["chain_status"] == "opened"
    assert "seal_damaged_recovered" in view["risk_flags"]
    assert [e["event_type"] for e in view["seal_events"]] == ["seal", "handover", "damage", "reseal", "handover", "receive", "open"]
    del opened, lab


# ------------------------------------------------------------- 前处理与测值
def test_pretreatment_required_and_fail_blocks_measurement(client, headers):
    sample = _ready_sample(client, headers)
    # 缺前处理
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/measurements", headers=headers,
                    json={"c14_age_bp": 2300, "c14_error_bp": 30})
    assert r.status_code == 409 and r.json()["error"]["code"] == "pretreatment_missing"
    # fail 结论阻止测值
    client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "fail", "risks": ["collagen yield too low"]})
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/measurements", headers=headers,
                    json={"c14_age_bp": 2300, "c14_error_bp": 30})
    assert r.status_code == 409 and r.json()["error"]["code"] == "pretreatment_failed"
    # 重新前处理通过后可测，且风险标记保留 caution/pass 历史
    client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "pass", "risks": []})
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/measurements", headers=headers,
                    json={"c14_age_bp": 2300, "c14_error_bp": 30})
    assert r.status_code == 201


def test_raw_measurement_is_immutable(client, headers):
    sample = _ready_sample(client, headers)
    client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "pass"})
    meas_no = _measure(client, headers, sample["lab_no"])
    # 原始测值与误差不可覆盖
    with pytest.raises(sqlite3.IntegrityError):
        db_connection().execute("UPDATE measurements SET c14_age_bp=? WHERE measurement_no=?", (9999, meas_no))
    db_connection().rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db_connection().execute("UPDATE measurements SET c14_error_bp=? WHERE measurement_no=?", (1, meas_no))
    db_connection().rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db_connection().execute("DELETE FROM measurements WHERE measurement_no=?", (meas_no,))
    db_connection().rollback()
    # HTTP 层也没有修改入口：重复编号被拒绝
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/measurements", headers=headers,
                    json={"measurement_no": meas_no, "c14_age_bp": 1000, "c14_error_bp": 10})
    assert r.status_code == 409 and r.json()["error"]["code"] == "measurement_exists"


# ----------------------------------------------------------------- 校准任务
def test_calibration_idempotent_failure_recovery_and_input_record(client, headers):
    sample = _ready_sample(client, headers)
    client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "pass"})
    meas = _measure(client, headers, sample["lab_no"])

    body = {"measurement_no": meas, "curve_name": "INTCAL23-MINI"}
    first = client.post(f"/api/chrono/samples/{sample['lab_no']}/calibrations", headers=headers, json=body)
    second = client.post(f"/api/chrono/samples/{sample['lab_no']}/calibrations", headers=headers, json=body)
    assert first.status_code == second.status_code == 201
    a, b = first.json(), second.json()
    assert a["task_no"] == b["task_no"] and a["input_hash"] == b["input_hash"]
    # 输入摘要与算法版本被保存
    assert a["input_summary"]["curve"]["name"] == "INTCAL23-MINI"
    assert a["input_summary"]["curve"]["checksum_sha256"]
    assert a["algorithm"] == {"name": "grid-gaussian-hpd", "version": "1.0.0"}
    assert a["input_summary"]["measurement"]["c14_age_bp"] == 2300.0
    # HTTP 展示置信区间、多峰标记
    assert a["hpd68"] and a["hpd95"]
    assert all({"start", "end", "probability", "peak"} <= set(iv) for iv in a["hpd68"])

    # 越界参数 -> 失败被持久化（失败恢复的起点）
    failed = client.post(
        f"/api/chrono/samples/{sample['lab_no']}/calibrations", headers=headers,
        json={"measurement_no": meas, "curve_name": "INTCAL23-MINI", "reservoir_offset_bp": -1000},
    ).json()
    assert failed["status"] == "failed" and failed["error"]
    fetched_failed = client.get(f"/api/chrono/calibrations/{failed['task_no']}", headers=headers).json()
    assert fetched_failed["status"] == "failed"
    # 恢复：换用合法参数提交，得到成功的新任务
    recovered = _calibrate(client, headers, sample["lab_no"], meas, reservoir_offset_bp=100)
    assert recovered["status"] == "done"
    assert recovered["task_no"] != failed["task_no"]
    # 失败任务不能发布
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/publication", headers=headers,
                    json={"task_no": failed["task_no"], "decision": "adopted"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "calibration_failed"


# ----------------------------------------------------------------- 发布守卫
def test_publication_guards_and_immutability(client, headers):
    sample = _ready_sample(client, headers)
    client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "pass"})
    meas = _measure(client, headers, sample["lab_no"], age=2012, error=20)  # 贴年轻边界
    cal = _calibrate(client, headers, sample["lab_no"], meas)
    assert cal["truncated_at_curve_boundary"] is True
    # 边界截断时采纳必须显式确认
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/publication", headers=headers,
                    json={"task_no": cal["task_no"], "decision": "adopted"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "boundary_ack_required"
    # 拒绝决定不要求确认
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/publication", headers=headers,
                    json={"task_no": cal["task_no"], "decision": "rejected", "reason": "边界截断不可用"})
    assert r.status_code == 201
    # 发布后不可变：不能再次发布
    again = client.post(f"/api/chrono/samples/{sample['lab_no']}/publication", headers=headers,
                        json={"task_no": cal["task_no"], "decision": "adopted", "boundary_acknowledged": True})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_published"
    view = client.get(f"/api/chrono/samples/{sample['lab_no']}", headers=headers).json()
    assert view["publication"]["immutable"] is True
    assert view["publication"]["decision"] == "rejected"
    assert "date_rejected" in view["risk_flags"]
    # 数据库层不可更新/删除
    with pytest.raises(sqlite3.IntegrityError):
        db_connection().execute("UPDATE publications SET decision='adopted'")
    db_connection().rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db_connection().execute("DELETE FROM publications")
    db_connection().rollback()


def test_publish_blocked_when_chain_broken(client, headers):
    sample = _ready_sample(client, headers)
    lab = sample["lab_no"]
    client.post(f"/api/chrono/samples/{lab}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "pass"})
    meas = _measure(client, headers, lab)
    cal = _calibrate(client, headers, lab, meas)
    # 开封后发现破损：样品重新隔离，禁止直接发布
    r = client.post(f"/api/chrono/samples/{lab}/seal-events", headers=headers,
                    json={"event_type": "damage", "note": "开封时发现内袋封签异常"})
    assert r.status_code == 201 and r.json()["chain_status"] == "quarantined"
    r = client.post(f"/api/chrono/samples/{lab}/publication", headers=headers,
                    json={"task_no": cal["task_no"], "decision": "adopted"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "chain_incomplete"
    # 任务本身仍可查询，但风险标记应反映未解决的破损
    assert "seal_damaged_unresolved" in client.get(f"/api/chrono/samples/{lab}", headers=headers).json()["risk_flags"]
    # 恢复：重新封签并重新走完交接与开封，之后才能发布
    for step in (
        {"event_type": "reseal", "seal_id": "SEAL-R"},
        {"event_type": "handover", "from_party": "库房", "to_party": "实验室"},
        {"event_type": "receive", "actor": "王五"},
        {"event_type": "open", "actor": "王五"},
    ):
        rr = client.post(f"/api/chrono/samples/{lab}/seal-events", headers=headers, json=step)
        assert rr.status_code == 201, rr.text
    r = client.post(f"/api/chrono/samples/{lab}/publication", headers=headers,
                    json={"task_no": cal["task_no"], "decision": "adopted", "reason": "重新封签交接后确认"})
    assert r.status_code == 201
    assert "seal_damaged_recovered" in r.json()["risk_flags"]


# ------------------------------------------------------------- 阶段证据集
def _adopted_sample(client, headers, *, age=2300, error=30, **kw):
    sample = _ready_sample(client, headers, **kw)
    client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "pass", "risks": kw.get("risks", [])})
    meas = _measure(client, headers, sample["lab_no"], age=age, error=error)
    cal = _calibrate(client, headers, sample["lab_no"], meas)
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/publication", headers=headers,
                    json={"task_no": cal["task_no"], "decision": "adopted", "reason": "可用",
                          "boundary_acknowledged": cal["truncated_at_curve_boundary"]})
    assert r.status_code == 201, r.text
    return sample, cal


def test_phase_versions_diff_and_published_immutability(client, headers):
    s1, cal1 = _adopted_sample(client, headers, field_code="P-1")
    s2, _cal2 = _adopted_sample(client, headers, age=2850, error=25, field_code="P-2")

    ph = client.post("/api/chrono/phases", headers=headers, json={"phase_code": "PH1", "name": "阶段一"})
    assert ph.status_code == 201

    # 成员乱序提交 -> 稳定排序后按 sample_id 排位置
    v1 = client.post("/api/chrono/phases/PH1/versions", headers=headers,
                     json={"curve_name": "INTCAL23-MINI", "members": [{"sample_id": s2["sample_id"]}, {"sample_id": s1["sample_id"]}]})
    assert v1.status_code == 201, v1.text
    v1j = v1.json()
    assert [m["sample_id"] for m in v1j["members"]] == sorted(m["sample_id"] for m in v1j["members"])
    assert v1j["version_no"] == 1 and v1j["status"] == "draft"

    # 相同输入重复提交 -> 同一版本
    again = client.post("/api/chrono/phases/PH1/versions", headers=headers,
                        json={"curve_name": "INTCAL23-MINI", "members": [{"sample_id": s1["sample_id"]}, {"sample_id": s2["sample_id"]}]})
    assert again.json()["version_no"] == 1 and again.json()["input_hash"] == v1j["input_hash"]

    # 发布 v1 后不可变
    pub = client.post("/api/chrono/phases/PH1/versions/1/publish", headers=headers)
    assert pub.status_code == 200 and pub.json()["immutable"] is True
    with pytest.raises(sqlite3.IntegrityError):
        db_connection().execute("UPDATE phase_members SET excluded=1 WHERE phase_version_id=1")
    db_connection().rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db_connection().execute("DELETE FROM phase_versions WHERE id=1")
    db_connection().rollback()

    # v2：s1 排除（必须给理由），与 v1 可比较
    v2 = client.post(
        "/api/chrono/phases/PH1/versions", headers=headers,
        json={"curve_name": "INTCAL23-MINI", "members": [
            {"sample_id": s1["sample_id"], "excluded": True, "exclude_reason": "地层扰动"},
            {"sample_id": s2["sample_id"]},
        ]},
    )
    assert v2.status_code == 201 and v2.json()["version_no"] == 2
    diff12 = client.get("/api/chrono/phases/PH1/diff?from=1&to=2", headers=headers).json()
    assert diff12["curve_changed"] is False
    assert diff12["changed"][0]["sample_id"] == s1["sample_id"]
    assert diff12["changed"][0]["exclusion"]["to"]["excluded"] is True

    # v3：换海洋曲线（成员需先有该曲线的成功校准）；没有时明确报错
    missing = client.post("/api/chrono/phases/PH1/versions", headers=headers,
                          json={"curve_name": "MARINE23-MINI", "members": [{"sample_id": s1["sample_id"]}, {"sample_id": s2["sample_id"]}]})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "member_not_calibrated"
    # 为两个样品补海洋测值与校准
    marine_jobs = {}
    for s in (s1, s2):
        m = _measure(client, headers, s["lab_no"], age=2700, error=30)
        marine_jobs[s["sample_id"]] = _calibrate(client, headers, s["lab_no"], m, curve="MARINE23-MINI")["task_no"]
    v3 = client.post("/api/chrono/phases/PH1/versions", headers=headers,
                     json={"curve_name": "MARINE23-MINI", "members": [{"sample_id": s1["sample_id"]}, {"sample_id": s2["sample_id"]}]})
    assert v3.status_code == 201 and v3.json()["version_no"] == 3 and v3.json()["curve_name"] == "MARINE23-MINI"
    diff13 = client.get("/api/chrono/phases/PH1/diff?from=1&to=3", headers=headers).json()
    assert diff13["curve"] == {"from": "INTCAL23-MINI", "to": "MARINE23-MINI"}
    assert diff13["curve_changed"] is True
    changed_ids = {c["sample_id"] for c in diff13["changed"]}
    assert changed_ids == {s1["sample_id"], s2["sample_id"]}

    # 版本视图展示置信区间、来源、风险标记
    view = client.get("/api/chrono/phases/PH1/versions/2", headers=headers).json()
    member = view["members"][0]
    assert member["source"]["context_name"]
    assert member["calibration"]["hpd95"][0]["start_label"].endswith(("BCE", "CE"))
    assert isinstance(member["risk_flags"], list)


def test_excluded_member_requires_reason_and_rejected_cannot_be_included(client, headers):
    s1, _ = _adopted_sample(client, headers, field_code="A-1")
    # 第二个样品研究者决定 rejected
    s2 = _ready_sample(client, headers, field_code="A-2")
    client.post(f"/api/chrono/samples/{s2['lab_no']}/pretreatments", headers=headers,
                json={"method": "AAA", "operator": "x", "conclusion": "caution", "risks": ["rootlets"]})
    meas = _measure(client, headers, s2["lab_no"], age=2850, error=25)
    cal = _calibrate(client, headers, s2["lab_no"], meas)
    client.post(f"/api/chrono/samples/{s2['lab_no']}/publication", headers=headers,
                json={"task_no": cal["task_no"], "decision": "rejected", "reason": "根须污染"})

    client.post("/api/chrono/phases", headers=headers, json={"phase_code": "PH2", "name": "阶段二"})
    # rejected 成员直接纳入 -> 拒绝
    r = client.post("/api/chrono/phases/PH2/versions", headers=headers,
                    json={"curve_name": "INTCAL23-MINI", "members": [{"sample_id": s1["sample_id"]}, {"sample_id": s2["sample_id"]}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "member_rejected"
    # 排除但没有理由 -> 拒绝
    r = client.post("/api/chrono/phases/PH2/versions", headers=headers,
                    json={"curve_name": "INTCAL23-MINI", "members": [{"sample_id": s1["sample_id"]}, {"sample_id": s2["sample_id"], "excluded": True}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "exclude_reason_required"
    # 带理由排除 -> 成功，计数正确
    r = client.post("/api/chrono/phases/PH2/versions", headers=headers,
                    json={"curve_name": "INTCAL23-MINI", "members": [
                        {"sample_id": s1["sample_id"]},
                        {"sample_id": s2["sample_id"], "excluded": True, "exclude_reason": "根须污染排除"},
                    ]})
    assert r.status_code == 201
    assert r.json()["included_count"] == 1 and r.json()["excluded_count"] == 1


def test_unknown_curve_and_sample_return_404(client, headers):
    assert client.get("/api/chrono/samples/LAB-999999", headers=headers).status_code == 404
    sample = _ready_sample(client, headers)
    client.post(f"/api/chrono/samples/{sample['lab_no']}/pretreatments", headers=headers,
                json={"method": "ABA", "operator": "x", "conclusion": "pass"})
    meas = _measure(client, headers, sample["lab_no"])
    r = client.post(f"/api/chrono/samples/{sample['lab_no']}/calibrations", headers=headers,
                    json={"measurement_no": meas, "curve_name": "INTCAL99-FULL"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "curve_not_found"
