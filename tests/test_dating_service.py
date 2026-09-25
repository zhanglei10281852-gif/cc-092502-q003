"""年代证据服务 HTTP/流程测试：样品身份链路、封签闸门、测值不可变、
校准幂等、采用决定、阶段证据集版本化与版本差异。"""
from __future__ import annotations

import pytest


def _login(client, username="owner", password="OwnerPass!234"):
    client.post("/api/users", json={"username": username, "display_name": "负责人", "password": password})
    token = client.post("/api/sessions", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def env(client):
    headers = _login(client)
    project_id = client.post("/api/projects", json={"code": "SITE1", "name": "一期", "site_name": "某地"}, headers=headers).json()["id"]
    return {"client": client, "headers": headers, "project_id": project_id}


def _register(client, headers, project_id, lab_no, *, context=("stratum", "T1第4层"), custodian="发掘队"):
    response = client.post("/api/dating/samples", json={
        "lab_no": lab_no, "project_id": project_id,
        "context_type": context[0], "context_name": context[1],
        "material": "炭化种子", "collected_at": "2026-09-01", "custodian": custodian,
    }, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _transfer_to_lab(client, headers, lab_no, frm="发掘队"):
    response = client.post(f"/api/dating/samples/{lab_no}/seal-events", json={
        "event_type": "transfer", "from_party": frm, "to_party": "年代学实验室",
    }, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _measure_and_calibrate(client, headers, lab_no, age=2500, sigma=30):
    created = client.post(f"/api/dating/samples/{lab_no}/measurements", json={
        "c14_age": age, "c14_sigma": sigma, "method": "AMS",
        "instrument": "MICADAS", "measured_by": "操作员A", "measured_at": "2026-09-10",
    }, headers=headers)
    assert created.status_code == 201, created.text
    measurement = created.json()
    task = client.post(f"/api/dating/samples/{lab_no}/measurements/{measurement['id']}/calibrate",
                       json={}, headers=headers)
    assert task.status_code == 201, task.text
    return measurement, task.json()


def _ready_sample(client, headers, project_id, lab_no, *, age=2500, sigma=30, **kwargs):
    _register(client, headers, project_id, lab_no, **kwargs)
    _transfer_to_lab(client, headers, lab_no)
    return _measure_and_calibrate(client, headers, lab_no, age=age, sigma=sigma)


# ---------------------------------------------------------------------------
# 取样登记与封签链路
# ---------------------------------------------------------------------------

def test_register_creates_sealed_sample_with_identity_chain(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    sample = _register(client, headers, pid, "ba-0001")
    assert sample["lab_no"] == "BA-0001"  # 规范化为大写
    assert sample["seal_state"] == "intact"
    assert sample["chain_ok"] is True
    assert sample["publishable"] is True
    assert len(sample["seal_events"]) == 1
    assert sample["seal_events"][0]["event_type"] == "seal"
    # 样品来源信息可展示
    assert sample["context_type"] == "stratum"
    assert sample["context_name"] == "T1第4层"


def test_duplicate_lab_no_rejected(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    _register(client, headers, pid, "BA-0100")
    duplicate = client.post(
        "/api/dating/samples", json={
            "lab_no": "ba-0100", "project_id": pid, "context_type": "stratum",
            "context_name": "x", "material": "炭", "collected_at": "2026-09-01", "custodian": "队",
        }, headers=headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "sample_exists"


def test_transfer_requires_continuous_custody(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    _register(client, headers, pid, "BA-0002")
    # 缺少接收方
    missing = client.post("/api/dating/samples/BA-0002/seal-events",
                          json={"event_type": "transfer", "from_party": "发掘队"}, headers=headers)
    assert missing.status_code == 422
    # 交出方与当前保管人不一致 -> 视为跳过交接环节
    gap = client.post("/api/dating/samples/BA-0002/seal-events", json={
        "event_type": "transfer", "from_party": "第三方", "to_party": "年代学实验室",
    }, headers=headers)
    assert gap.status_code == 409
    assert gap.json()["error"]["code"] == "custodian_gap"
    # 正常交接后再交接：上一接收方才能交出
    _transfer_to_lab(client, headers, "BA-0002")
    onward = client.post("/api/dating/samples/BA-0002/seal-events", json={
        "event_type": "transfer", "from_party": "发掘队", "to_party": "归档室",
    }, headers=headers)
    assert onward.status_code == 409
    ok = client.post("/api/dating/samples/BA-0002/seal-events", json={
        "event_type": "transfer", "from_party": "年代学实验室", "to_party": "归档室",
    }, headers=headers)
    assert ok.status_code == 201 and ok.json()["current_custodian"] == "归档室"


def test_seal_events_append_only(env):
    from app.database import connection
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    _register(client, headers, pid, "BA-0003")
    import sqlite3
    with pytest.raises(sqlite3.Error):
        connection().execute("UPDATE seal_events SET to_party='X' WHERE seq=1")
    with pytest.raises(sqlite3.Error):
        connection().execute("DELETE FROM seal_events WHERE seq=1")


# ---------------------------------------------------------------------------
# 风险标记与封签破损闸门
# ---------------------------------------------------------------------------

def test_broken_seal_blocks_adoption_and_reseal_recovers(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    _register(client, headers, pid, "BA-0010")
    _transfer_to_lab(client, headers, "BA-0010")
    measurement, task = _measure_and_calibrate(client, headers, "BA-0010")
    # 完好状态不能重封
    reseal_when_intact = client.post("/api/dating/samples/BA-0010/seal-events",
                                     json={"event_type": "reseal"}, headers=headers)
    assert reseal_when_intact.status_code == 409 and reseal_when_intact.json()["error"]["code"] == "not_broken"
    # 封签破损
    broken = client.post("/api/dating/samples/BA-0010/seal-events",
                         json={"event_type": "break", "note": "运输破损"}, headers=headers)
    assert broken.status_code == 201 and broken.json()["seal_state"] == "broken"
    detail = client.get("/api/dating/samples/BA-0010", headers=headers).json()
    assert detail["chain_ok"] is False and detail["publishable"] is False
    # 破损状态下禁止采用结果，也不能重复报破损
    blocked = client.post("/api/dating/samples/BA-0010/decisions",
                          json={"calibration_task_id": task["id"], "decision": "adopted"}, headers=headers)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "seal_chain_broken"
    double_break = client.post("/api/dating/samples/BA-0010/seal-events",
                               json={"event_type": "break"}, headers=headers)
    assert double_break.status_code == 409 and double_break.json()["error"]["code"] == "already_broken"
    # 重新加封后链路恢复，可采用
    resealed = client.post("/api/dating/samples/BA-0010/seal-events",
                           json={"event_type": "reseal", "note": "复检重封"}, headers=headers)
    assert resealed.status_code == 201 and resealed.json()["seal_state"] == "intact"
    recovered = client.get("/api/dating/samples/BA-0010", headers=headers).json()
    assert recovered["chain_ok"] is True and recovered["publishable"] is True
    adopted = client.post("/api/dating/samples/BA-0010/decisions",
                          json={"calibration_task_id": task["id"], "decision": "adopted",
                                "reason": "链路恢复后采用"}, headers=headers)
    assert adopted.status_code == 201


def test_open_high_risk_blocks_until_resolved(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    _register(client, headers, pid, "BA-0020")
    _transfer_to_lab(client, headers, "BA-0020")
    _, task = _measure_and_calibrate(client, headers, "BA-0020")
    risk = client.post("/api/dating/samples/BA-0020/risks",
                       json={"risk_code": "ROOTLETS", "severity": "high", "note": "根系侵入"}, headers=headers)
    assert risk.status_code == 201
    detail = client.get("/api/dating/samples/BA-0020", headers=headers).json()
    assert detail["high_risk_open"] is True and detail["publishable"] is False
    blocked = client.post("/api/dating/samples/BA-0020/decisions",
                          json={"calibration_task_id": task["id"], "decision": "adopted"}, headers=headers)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "open_high_risk"
    # 处置风险后放行
    resolved = client.post(f"/api/dating/samples/BA-0020/risks/{risk.json()['id']}/resolve",
                           json={"resolution_note": "机械去除并复核"}, headers=headers)
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    adopted = client.post("/api/dating/samples/BA-0020/decisions",
                          json={"calibration_task_id": task["id"], "decision": "adopted"}, headers=headers)
    assert adopted.status_code == 201
    # 已处置记录不可重复处置
    again = client.post(f"/api/dating/samples/BA-0020/risks/{risk.json()['id']}/resolve",
                        json={}, headers=headers)
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# 原始测值与校准任务
# ---------------------------------------------------------------------------

def test_raw_measurements_append_only_and_used_for_calibration(env):
    import sqlite3
    from app.database import connection
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    measurement, task = _ready_sample(client, headers, pid, "BA-0030")
    assert task["status"] == "completed"
    assert task["c14_age"] == 2500 and task["c14_sigma"] == 30
    assert task["input_summary"]["algorithm_version"]
    assert task["input_summary"]["curve_version"] == "mini-1"
    # 置信区间与多峰摘要存在
    result = task["result"]
    assert result["hpd_intervals"] and abs(
        sum(i["probability"] for i in result["hpd_intervals"]) - 0.954) < 2e-3
    assert result["modes"]
    # 原始测值不可更新/删除
    with pytest.raises(sqlite3.Error):
        connection().execute("UPDATE measurements SET c14_age=1 WHERE id=?", (measurement["id"],))
    with pytest.raises(sqlite3.Error):
        connection().execute("DELETE FROM measurements WHERE id=?", (measurement["id"],))
    # 同一样品可追加第二测值，两测值独立存在
    second = client.post("/api/dating/samples/BA-0030/measurements", json={
        "c14_age": 2520, "c14_sigma": 35, "method": "AMS", "measured_at": "2026-09-12",
    }, headers=headers)
    assert second.status_code == 201 and second.json()["id"] != measurement["id"]
    detail = client.get("/api/dating/samples/BA-0030", headers=headers).json()
    assert len(detail["measurements"]) == 2


def test_calibration_tasks_idempotent_and_immutable(env):
    import sqlite3
    from app.database import connection
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    measurement, task = _ready_sample(client, headers, pid, "BA-0040")
    # 相同输入重复提交返回同一任务（直提与按测值提交共享）
    direct = client.post("/api/dating/calibrations", json={"c14_age": 2500, "c14_sigma": 30})
    direct_again = client.post("/api/dating/calibrations", json={"c14_age": 2500, "c14_sigma": 30})
    assert direct.status_code == direct_again.status_code == 201
    assert direct.json()["id"] == direct_again.json()["id"] == task["id"]
    # 不同误差是不同任务
    other = client.post("/api/dating/calibrations", json={"c14_age": 2500, "c14_sigma": 31})
    assert other.json()["id"] != task["id"]
    # 校准任务记录不可更新
    with pytest.raises(sqlite3.Error):
        connection().execute("UPDATE calibration_tasks SET status='failed' WHERE id=?", (task["id"],))


def test_failed_calibration_persisted_and_recovery(env):
    client = env["client"]
    headers = env["headers"]
    pid = env["project_id"]
    body = {"c14_age": 12000, "c14_sigma": 30}
    failed = client.post("/api/dating/calibrations", json=body)
    assert failed.status_code == 201
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "out_of_support"
    again = client.post("/api/dating/calibrations", json=dict(body))
    assert again.status_code == 201, again.text
    assert again.json()["id"] == failed.json()["id"]
    ok = client.post("/api/dating/calibrations", json={"c14_age": 2500, "c14_sigma": 30})
    assert ok.json()["status"] == "completed"
    assert ok.json()["id"] != failed.json()["id"]
    _register(client, headers, pid, "BA-0050")
    _transfer_to_lab(client, headers, "BA-0050")
    bad_adopt = client.post("/api/dating/samples/BA-0050/decisions",
                            json={"calibration_task_id": failed.json()["id"], "decision": "adopted"},
                            headers=headers)
    assert bad_adopt.status_code in (409, 422)


def test_decisions_adopt_reject_and_supersede(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    _, task = _ready_sample(client, headers, pid, "BA-0060")
    first = client.post("/api/dating/samples/BA-0060/decisions",
                        json={"calibration_task_id": task["id"], "decision": "adopted", "reason": "首选"}, headers=headers)
    assert first.status_code == 201
    # 新决定取代旧决定，历史保留
    second = client.post("/api/dating/samples/BA-0060/decisions",
                         json={"decision": "rejected", "reason": "改用其他测值"}, headers=headers)
    assert second.status_code == 201
    rows = second.json()["data"]
    assert len(rows) == 2
    assert rows[0]["decision"] == "adopted" and rows[0]["superseded_by_id"] == rows[1]["id"]
    assert rows[1]["decision"] == "rejected" and rows[1]["superseded_by_id"] is None
    # 采用决定必须指定任务
    missing = client.post("/api/dating/samples/BA-0060/decisions",
                          json={"decision": "adopted"}, headers=headers)
    assert missing.status_code == 422


# ---------------------------------------------------------------------------
# 阶段证据集：版本化、发布不可变、版本差异
# ---------------------------------------------------------------------------

def _phase_with_three_samples(client, headers, pid):
    _, t1 = _ready_sample(client, headers, pid, "BA-1001", context=("stratum", "L4"), age=2500, sigma=30)
    _, t2 = _ready_sample(client, headers, pid, "BA-1002", context=("pit", "H7"), age=2470, sigma=35)
    _register(client, headers, pid, "BA-1003", context=("pit", "H9"))
    _transfer_to_lab(client, headers, "BA-1003")
    client.post("/api/dating/samples/BA-1003/seal-events", json={"event_type": "break", "note": "破损"}, headers=headers)
    m3 = client.post("/api/dating/samples/BA-1003/measurements", json={
        "c14_age": 2600, "c14_sigma": 35, "method": "AMS", "measured_at": "2026-09-12"}, headers=headers).json()
    client.post(f"/api/dating/samples/BA-1003/measurements/{m3['id']}/calibrate", json={}, headers=headers)
    phase_set = client.post("/api/dating/phase-sets", json={"project_id": pid, "name": "T1阶段"}, headers=headers).json()
    return phase_set, t1, t2


def test_phase_version_publish_immutable_and_members_sorted(env):
    import sqlite3
    from app.database import connection
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    phase_set, _, _ = _phase_with_three_samples(client, headers, pid)
    created = client.post(f"/api/dating/phase-sets/{phase_set['id']}/versions", json={"members": [
        {"sample_lab_no": "BA-1003", "included": False, "exclusion_reason": "封签破损"},
        {"sample_lab_no": "BA-1001"},
        {"sample_lab_no": "BA-1002"},
    ]}, headers=headers)
    assert created.status_code == 201, created.text
    version = created.json()
    # 成员按实验室编号稳定排序
    assert [m["lab_no"] for m in version["members"]] == ["BA-1001", "BA-1002", "BA-1003"]
    # 草稿预览给出组合分布
    assert version["combined_preview"]["hpd_intervals"]
    published = client.post(f"/api/dating/phase-versions/{version['id']}/publish",
                            json={"note": "正式发布"}, headers=headers)
    assert published.status_code == 200 and published.json()["status"] == "published"
    combined = published.json()["combined"]
    assert abs(sum(i["probability"] for i in combined["hpd_intervals"]) - 0.954) < 2e-3
    # 服务层拒绝重复发布
    again = client.post(f"/api/dating/phase-versions/{version['id']}/publish", json={}, headers=headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "version_published"
    # 数据库层也保证已发布版本不可变
    with pytest.raises(sqlite3.Error):
        connection().execute("UPDATE phase_versions SET status='draft' WHERE id=?", (version["id"],))


def test_phase_exclusion_requires_reason_and_broken_member_blocked(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    phase_set, _, _ = _phase_with_three_samples(client, headers, pid)
    no_reason = client.post(f"/api/dating/phase-sets/{phase_set['id']}/versions",
                            json={"members": [{"sample_lab_no": "BA-1003", "included": False}]}, headers=headers)
    assert no_reason.status_code == 422 and no_reason.json()["error"]["code"] == "exclusion_reason_required"
    broken_included = client.post(f"/api/dating/phase-sets/{phase_set['id']}/versions",
                                  json={"members": [{"sample_lab_no": "BA-1003"}]}, headers=headers)
    blocked = client.post(f"/api/dating/phase-versions/{broken_included.json()['id']}/publish",
                          json={}, headers=headers)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "seal_chain_broken"


def test_new_version_on_member_change_and_comparison(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    phase_set, _, _ = _phase_with_three_samples(client, headers, pid)
    v1 = client.post(f"/api/dating/phase-sets/{phase_set['id']}/versions", json={"members": [
        {"sample_lab_no": "BA-1001"}, {"sample_lab_no": "BA-1002"},
        {"sample_lab_no": "BA-1003", "included": False, "exclusion_reason": "封签破损"},
    ]}, headers=headers).json()
    client.post(f"/api/dating/phase-versions/{v1['id']}/publish", json={}, headers=headers)
    # 更换成员（移除 BA-1002）生成新版本
    v2 = client.post(f"/api/dating/phase-sets/{phase_set['id']}/versions", json={"members": [
        {"sample_lab_no": "BA-1001"},
        {"sample_lab_no": "BA-1003", "included": False, "exclusion_reason": "封签破损"},
    ]}, headers=headers).json()
    assert v2["version_no"] == 2 and v2["based_on_version_id"] == v1["id"]
    comparison = client.get(f"/api/dating/phase-versions/{v1['id']}/compare/{v2['id']}", headers=headers).json()
    assert comparison["members_removed"] == ["BA-1002"]
    assert comparison["members_added"] == []
    assert comparison["hpd_changed"] is True
    assert comparison["curve_changed"] is False
    # 完全相同的成员/曲线去重到既有版本
    same = client.post(f"/api/dating/phase-sets/{phase_set['id']}/versions", json={"members": [
        {"sample_lab_no": "BA-1001"},
        {"sample_lab_no": "BA-1003", "included": False, "exclusion_reason": "封签破损"},
    ]}, headers=headers)
    assert same.json()["id"] == v2["id"]
    # 证据集视图按版本号稳定列出
    detail = client.get(f"/api/dating/phase-sets/{phase_set['id']}", headers=headers).json()
    assert [v["version_no"] for v in detail["versions"]] == [1, 2]
    assert detail["versions"][0]["status"] == "published"


def test_unknown_curve_rejected(env):
    client, headers, pid = env["client"], env["headers"], env["project_id"]
    phase_set, _, _ = _phase_with_three_samples(client, headers, pid)
    bad = client.post(f"/api/dating/phase-sets/{phase_set['id']}/versions", json={
        "curve_version": "IntCalX", "members": [{"sample_lab_no": "BA-1001"}],
    }, headers=headers)
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "unknown_curve"


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------

def test_non_member_and_viewer_permissions(env, client):
    headers, pid = env["headers"], env["project_id"]
    # 非项目成员不能登记样品
    other = _login(client, "recorder2", "Recorder!2345")
    denied = client.post("/api/dating/samples", json={
        "lab_no": "BA-9001", "project_id": pid, "context_type": "stratum",
        "context_name": "x", "material": "炭", "collected_at": "2026-09-01", "custodian": "队",
    }, headers=other)
    assert denied.status_code == 403
    # viewer 加入项目后可读不可写
    user = client.post("/api/users", json={"username": "viewer1", "display_name": "观察者", "password": "Viewer!23456"}).json()
    client.post(f"/api/projects/{pid}/members", json={"user_id": user["id"], "role": "viewer"}, headers=headers)
    vh = _login(client, "viewer1", "Viewer!23456")
    can_read = client.get("/api/dating/samples", headers=vh)
    assert can_read.status_code == 200
    cannot_write = client.post("/api/dating/samples", json={
        "lab_no": "BA-9002", "project_id": pid, "context_type": "stratum",
        "context_name": "x", "material": "炭", "collected_at": "2026-09-01", "custodian": "队",
    }, headers=vh)
    assert cannot_write.status_code == 403


def test_requires_authentication(client):
    assert client.get("/api/dating/samples").status_code == 401
    assert client.post("/api/dating/phase-sets", json={"project_id": 1, "name": "x"}).status_code == 401
