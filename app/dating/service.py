"""年代证据服务：样品身份连续、封签链路、前处理风险、测值、校准、采用决定、阶段证据集。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import connection, now, transaction
from app.security import request_hash, stable_json
from app.dating.calibration import (
    ALGORITHM_VERSION,
    CURVE_VERSION,
    DEFAULT_PROBABILITY,
    CalibrationError,
    CalibrationInput,
    UnknownCurveError,
    calibrate,
    combine_calibrated,
    curve_info,
    curve_series,
)


class DatingError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _row(db: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return db.execute(sql, params).fetchone()


def _require_project_member(db: sqlite3.Connection, project_id: int, user_id: int) -> str:
    row = _row(db, "SELECT role FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id))
    if row is None:
        raise DatingError("forbidden", "当前用户不是该项目成员", 403)
    return row["role"]


def _require_editor(db: sqlite3.Connection, project_id: int, user_id: int) -> str:
    role = _require_project_member(db, project_id, user_id)
    if role not in {"owner", "researcher", "recorder"}:
        raise DatingError("forbidden", "当前角色不能执行该写入操作", 403)
    return role


def _audit(db: sqlite3.Connection, action: str, resource_id: str, payload: dict[str, Any],
           *, project_id: int | None = None, actor_id: int | None = None) -> None:
    from app.security import sanitize
    db.execute(
        "INSERT INTO audit_events(project_id,actor_id,action,resource_type,resource_id,payload_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (project_id, actor_id, action, "dating", resource_id, stable_json(sanitize(payload)), now()),
    )


# ---------------------------------------------------------------------------
# 校准曲线
# ---------------------------------------------------------------------------

def list_curves() -> dict[str, Any]:
    info = curve_info(CURVE_VERSION)
    return {
        "data": [{
            "version": info.version,
            "name": info.name,
            "min_cal_bp": info.min_cal_bp,
            "max_cal_bp": info.max_cal_bp,
            "grid_step": info.grid_step,
            "points": info.points,
            "algorithm_versions": [ALGORITHM_VERSION],
        }]
    }


def get_curve(version: str) -> dict[str, Any]:
    try:
        return curve_series(version)
    except UnknownCurveError:
        raise DatingError("unknown_curve", "未内置的校准曲线版本", 404) from None


# ---------------------------------------------------------------------------
# 取样登记与封签链路
# ---------------------------------------------------------------------------

def register_sample(payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    _require_editor(db, payload["project_id"], actor_id)
    if _row(db, "SELECT 1 FROM projects WHERE id=?", (payload["project_id"],)) is None:
        raise DatingError("project_not_found", "项目不存在", 404)
    lab_no = payload["lab_no"].strip().upper()
    stamp = now()
    try:
        with transaction(immediate=True) as tx:
            tx.execute(
                "INSERT INTO dating_samples(lab_no,project_id,context_type,context_name,material,sample_note,"
                "collected_at,current_custodian,seal_state,registered_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (lab_no, payload["project_id"], payload["context_type"], payload["context_name"],
                 payload["material"], payload.get("sample_note", ""), payload["collected_at"],
                 payload["custodian"], "intact", actor_id, stamp, stamp),
            )
            tx.execute(
                "INSERT INTO seal_events(sample_lab_no,seq,event_type,from_party,to_party,seal_state,note,actor_id,created_at) "
                "VALUES(?,1,'seal','',?,'intact','取样登记并加封',?,?)",
                (lab_no, payload["custodian"], actor_id, stamp),
            )
            _audit(tx, "dating.sample.register", lab_no, payload, project_id=payload["project_id"], actor_id=actor_id)
    except sqlite3.IntegrityError as exc:
        raise DatingError("sample_exists", "实验室编号已存在", 409) from exc
    return get_sample(lab_no)


def _get_sample_row(db: sqlite3.Connection, lab_no: str) -> sqlite3.Row:
    row = _row(db, "SELECT * FROM dating_samples WHERE lab_no=?", (lab_no,))
    if row is None:
        raise DatingError("sample_not_found", "样品不存在", 404)
    return row


def add_seal_event(lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    sample = _get_sample_row(db, lab_no)
    _require_editor(db, sample["project_id"], actor_id)
    event_type = payload["event_type"]
    last = _row(db, "SELECT * FROM seal_events WHERE sample_lab_no=? ORDER BY seq DESC LIMIT 1", (lab_no,))
    new_state = {
        "seal": "intact",
        "transfer": last["seal_state"],
        "break": "broken",
        "reseal": "intact",
    }[event_type]
    if event_type == "transfer":
        if not payload.get("to_party"):
            raise DatingError("transfer_requires_party", "交接事件必须写明接收方", 422)
        if not payload.get("from_party"):
            raise DatingError("transfer_requires_party", "交接事件必须写明代交出方", 422)
        if payload["from_party"] != sample["current_custodian"]:
            raise DatingError(
                "custodian_gap",
                f"交出方 {payload['from_party']} 与当前保管人 {sample['current_custodian']} 不一致，不能跳过交接环节",
                409,
            )
    if event_type == "break" and sample["seal_state"] == "broken":
        raise DatingError("already_broken", "封签已处于破损状态，需先重新加封", 409)
    if event_type == "reseal" and sample["seal_state"] != "broken":
        raise DatingError("not_broken", "封签完好时不能重新加封", 409)
    if event_type == "seal" and last is not None:
        raise DatingError("already_sealed", "样品已有初始加封事件", 409)

    stamp = now()
    with transaction(immediate=True) as tx:
        seq = (last["seq"] + 1) if last else 1
        tx.execute(
            "INSERT INTO seal_events(sample_lab_no,seq,event_type,from_party,to_party,seal_state,note,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (lab_no, seq, event_type, payload.get("from_party", ""), payload.get("to_party", ""),
             new_state, payload.get("note", ""), actor_id, stamp),
        )
        custodian = payload["to_party"] if event_type in {"transfer", "seal"} and payload.get("to_party") else sample["current_custodian"]
        tx.execute("UPDATE dating_samples SET seal_state=?,current_custodian=?,updated_at=? WHERE lab_no=?",
                   (new_state, custodian, stamp, lab_no))
        _audit(tx, f"dating.seal.{event_type}", lab_no, {"seq": seq, **payload},
               project_id=sample["project_id"], actor_id=actor_id)
    return get_sample(lab_no)


def get_sample(lab_no: str) -> dict[str, Any]:
    db = connection()
    sample = _get_sample_row(db, lab_no)
    data = dict(sample)
    data["seal_events"] = [dict(row) for row in db.execute(
        "SELECT id,seq,event_type,from_party,to_party,seal_state,note,actor_id,created_at "
        "FROM seal_events WHERE sample_lab_no=? ORDER BY seq,id", (lab_no,)).fetchall()]
    data["risks"] = [dict(row) for row in db.execute(
        "SELECT * FROM pretreatment_risks WHERE sample_lab_no=? ORDER BY id", (lab_no,)).fetchall()]
    data["measurements"] = [dict(row) for row in db.execute(
        "SELECT id,sample_lab_no,c14_age,c14_sigma,method,instrument,measured_by,measured_at,created_at "
        "FROM measurements WHERE sample_lab_no=? ORDER BY id", (lab_no,)).fetchall()]
    data["open_risk_count"] = sum(1 for risk in data["risks"] if risk["status"] == "open")
    data["high_risk_open"] = any(risk["status"] == "open" and risk["severity"] == "high" for risk in data["risks"])
    data["chain_ok"] = _chain_ok(data["seal_events"])
    data["publishable"] = data["chain_ok"] and sample["seal_state"] == "intact" and not data["high_risk_open"]
    return data


def _chain_ok(events: list[dict[str, Any]] | list[sqlite3.Row]) -> bool:
    """身份连续性：重放封签事件序列。

    必须以 seal 起始；交接必须有接收方，交出方须等于当前保管人（不允许跳过环节）；
    break 后必须经 reseal 恢复，序列终点封签必须完好。
    """
    if not events or events[0]["event_type"] != "seal":
        return False
    custodian = events[0]["to_party"]
    intact = True
    expected_seq = 1
    for event in events:
        if event["seq"] != expected_seq:
            return False
        expected_seq += 1
        kind = event["event_type"]
        if kind == "seal":
            if expected_seq != 2:
                return False
        elif kind == "transfer":
            if not event["to_party"]:
                return False
            if event["from_party"] and custodian and event["from_party"] != custodian:
                return False
            custodian = event["to_party"] or custodian
        elif kind == "break":
            if not intact:
                return False
            intact = False
        elif kind == "reseal":
            if intact:
                return False
            intact = True
        if event["seal_state"] != ("intact" if intact else "broken"):
            return False
    return intact


def list_samples(project_id: int | None = None) -> dict[str, Any]:
    db = connection()
    if project_id is None:
        rows = db.execute("SELECT lab_no,project_id,context_type,context_name,material,current_custodian,seal_state,status FROM dating_samples ORDER BY lab_no").fetchall()
    else:
        rows = db.execute(
            "SELECT lab_no,project_id,context_type,context_name,material,current_custodian,seal_state,status "
            "FROM dating_samples WHERE project_id=? ORDER BY lab_no", (project_id,)).fetchall()
    return {"data": [dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# 前处理风险
# ---------------------------------------------------------------------------

def add_risk(lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    sample = _get_sample_row(db, lab_no)
    _require_editor(db, sample["project_id"], actor_id)
    stamp = now()
    with transaction(immediate=True) as tx:
        cursor = tx.execute(
            "INSERT INTO pretreatment_risks(sample_lab_no,risk_code,severity,note,status,actor_id,created_at) "
            "VALUES(?,?,?,?,'open',?,?)",
            (lab_no, payload["risk_code"], payload["severity"], payload.get("note", ""), actor_id, stamp))
        _audit(tx, "dating.risk.add", lab_no, {"risk_id": cursor.lastrowid, **payload},
               project_id=sample["project_id"], actor_id=actor_id)
        return dict(_row(tx, "SELECT * FROM pretreatment_risks WHERE id=?", (cursor.lastrowid,)))


def resolve_risk(lab_no: str, risk_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    sample = _get_sample_row(db, lab_no)
    _require_editor(db, sample["project_id"], actor_id)
    risk = _row(db, "SELECT * FROM pretreatment_risks WHERE id=? AND sample_lab_no=?", (risk_id, lab_no))
    if risk is None:
        raise DatingError("risk_not_found", "风险记录不存在", 404)
    if risk["status"] == "resolved":
        raise DatingError("risk_resolved", "风险已处置，记录不可改写", 409)
    stamp = now()
    with transaction(immediate=True) as tx:
        tx.execute(
            "UPDATE pretreatment_risks SET status='resolved',resolution_note=?,resolved_by=?,resolved_at=? WHERE id=?",
            (payload.get("resolution_note", ""), actor_id, stamp, risk_id))
        _audit(tx, "dating.risk.resolve", lab_no, {"risk_id": risk_id, **payload},
               project_id=sample["project_id"], actor_id=actor_id)
    return dict(_row(db, "SELECT * FROM pretreatment_risks WHERE id=?", (risk_id,)))


# ---------------------------------------------------------------------------
# 原始测值（只追加）与校准任务（幂等、结果不可变）
# ---------------------------------------------------------------------------

def add_measurement(lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    sample = _get_sample_row(db, lab_no)
    _require_editor(db, sample["project_id"], actor_id)
    if sample["status"] != "active":
        raise DatingError("sample_void", "样品已作废，不能再录入测值", 409)
    stamp = now()
    with transaction(immediate=True) as tx:
        cursor = tx.execute(
            "INSERT INTO measurements(sample_lab_no,c14_age,c14_sigma,method,instrument,measured_by,measured_at,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (lab_no, payload["c14_age"], payload["c14_sigma"], payload["method"],
             payload.get("instrument", ""), payload.get("measured_by", ""), payload["measured_at"], actor_id, stamp))
        mid = cursor.lastrowid
        _audit(tx, "dating.measurement.add", lab_no, {"measurement_id": mid, **payload},
               project_id=sample["project_id"], actor_id=actor_id)
    return dict(_row(db, "SELECT id,sample_lab_no,c14_age,c14_sigma,method,instrument,measured_by,measured_at,created_at FROM measurements WHERE id=?", (mid,)))


def _run_calibration(c14_age: float, c14_sigma: float, curve_version: str, probability: float) -> tuple[dict[str, Any], dict[str, Any]]:
    cal_input = CalibrationInput(c14_age, c14_sigma, curve_version, probability)
    input_summary = cal_input.digest_payload()
    input_summary["algorithm_version"] = ALGORITHM_VERSION
    try:
        result = calibrate(cal_input)
    except CalibrationError as exc:
        result = {"error": {"code": exc.code, "message": exc.message}}
    return input_summary, result


def _persist_calibration_task(measurement_id: int | None, c14_age: float, c14_sigma: float,
                               curve_version: str, probability: float, actor_id: int | None,
                               project_id: int | None) -> dict[str, Any]:
    """相同输入返回同一任务：以输入摘要的哈希为唯一键，结果只追加、永不覆盖。"""
    input_summary, result = _run_calibration(c14_age, c14_sigma, curve_version, probability)
    digest = request_hash(input_summary)
    failed = "error" in result
    db = connection()
    stamp = now()
    with transaction(immediate=True) as tx:
        existing = _row(tx, "SELECT * FROM calibration_tasks WHERE input_hash=?", (digest,))
        if existing is not None:
            task = dict(existing)
        else:
            cursor = tx.execute(
                "INSERT INTO calibration_tasks(input_hash,measurement_id,c14_age,c14_sigma,curve_version,probability,"
                "algorithm_version,input_summary_json,status,result_json,error_code,error_message,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (digest, measurement_id, c14_age, c14_sigma, curve_version, probability, ALGORITHM_VERSION,
                 stable_json(input_summary), "failed" if failed else "completed",
                 stable_json(result), result.get("error", {}).get("code", "") if failed else "",
                 result.get("error", {}).get("message", "") if failed else "", stamp),
            )
            task = dict(_row(tx, "SELECT * FROM calibration_tasks WHERE id=?", (cursor.lastrowid,)))
            _audit(tx, "dating.calibration.run", str(task["id"]),
                   {"input_hash": digest, "status": task["status"], "measurement_id": measurement_id},
                   project_id=project_id, actor_id=actor_id)
    return _load_task(task["id"])


def calibrate_direct(payload: dict[str, Any]) -> dict[str, Any]:
    curve_version = payload.get("curve_version") or CURVE_VERSION
    try:
        curve_info(curve_version)
    except UnknownCurveError:
        raise DatingError("unknown_curve", "未内置的校准曲线版本", 404) from None
    return _persist_calibration_task(None, payload["c14_age"], payload["c14_sigma"],
                                     curve_version, payload["probability"], None, None)


def calibrate_measurement(lab_no: str, measurement_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    sample = _get_sample_row(db, lab_no)
    _require_editor(db, sample["project_id"], actor_id)
    measurement = _row(db, "SELECT * FROM measurements WHERE id=? AND sample_lab_no=?", (measurement_id, lab_no))
    if measurement is None:
        raise DatingError("measurement_not_found", "该样品下不存在此测值", 404)
    curve_version = payload.get("curve_version") or CURVE_VERSION
    try:
        curve_info(curve_version)
    except UnknownCurveError:
        raise DatingError("unknown_curve", "未内置的校准曲线版本", 404) from None
    return _persist_calibration_task(measurement_id, measurement["c14_age"], measurement["c14_sigma"],
                                     curve_version, payload["probability"], actor_id, sample["project_id"])


def _load_task(task_id: int) -> dict[str, Any]:
    db = connection()
    row = _row(db, "SELECT * FROM calibration_tasks WHERE id=?", (task_id,))
    if row is None:
        raise DatingError("task_not_found", "校准任务不存在", 404)
    data = dict(row)
    data["input_summary"] = json.loads(data.pop("input_summary_json"))
    data["result"] = json.loads(data.pop("result_json")) if data["result_json"] else None
    return data


def get_task(task_id: int) -> dict[str, Any]:
    return _load_task(task_id)


# ---------------------------------------------------------------------------
# 发布闸门与研究者采用决定
# ---------------------------------------------------------------------------

def _ensure_publishable(db: sqlite3.Connection, sample: sqlite3.Row, task: dict[str, Any]) -> None:
    """链路可靠是讨论年代的前提：禁止跳过交接或封签破损后直接发布结果。"""
    detail = get_sample(sample["lab_no"])
    if not detail["chain_ok"] or sample["seal_state"] != "intact":
        raise DatingError("seal_chain_broken", "封签链路不连续或封签破损，不能采用/发布测年结果", 409)
    if detail["high_risk_open"]:
        raise DatingError("open_high_risk", "存在未处置的高风险前处理标记，不能采用/发布结果", 409)
    if task["status"] != "completed":
        raise DatingError("calibration_failed", "校准任务失败，没有可采用的结果", 409)


def create_decision(lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    sample = _get_sample_row(db, lab_no)
    role = _require_project_member(db, sample["project_id"], actor_id)
    if role not in {"owner", "researcher"}:
        raise DatingError("forbidden", "只有研究者可以作出采用决定", 403)

    task_id = payload.get("calibration_task_id")
    task: dict[str, Any] | None = None
    if payload["decision"] == "adopted":
        if task_id is None:
            raise DatingError("task_required", "采用决定必须指定校准任务", 422)
        task = _load_task(task_id)
        if not task["measurement_id"]:
            raise DatingError("task_not_measurement_bound", "采用的校准任务必须由该样品的原始测值生成", 422)
        m = _row(db, "SELECT * FROM measurements WHERE id=?", (task["measurement_id"],))
        if m is None or m["sample_lab_no"] != lab_no:
            raise DatingError("task_mismatch", "校准任务不属于该样品", 409)
        _ensure_publishable(db, sample, task)

    stamp = now()
    with transaction(immediate=True) as tx:
        cursor = tx.execute(
            "INSERT INTO determination_decisions(sample_lab_no,calibration_task_id,decision,reason,researcher_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (lab_no, task_id, payload["decision"], payload.get("reason", ""), actor_id, stamp))
        decision_id = cursor.lastrowid
        # 同一样品的旧决定被本次新决定取代，保留全部历史且不改写旧记录内容。
        tx.execute(
            "UPDATE determination_decisions SET superseded_by_id=? "
            "WHERE sample_lab_no=? AND id<>? AND superseded_by_id IS NULL",
            (decision_id, lab_no, decision_id))
        _audit(tx, f"dating.decision.{payload['decision']}", lab_no,
               {"decision_id": decision_id, "calibration_task_id": task_id, "reason": payload.get("reason", "")},
               project_id=sample["project_id"], actor_id=actor_id)
    return list_decisions(lab_no)


def list_decisions(lab_no: str) -> dict[str, Any]:
    db = connection()
    _get_sample_row(db, lab_no)
    rows = db.execute(
        "SELECT d.*, t.status AS task_status, t.curve_version, t.input_hash "
        "FROM determination_decisions d LEFT JOIN calibration_tasks t ON t.id=d.calibration_task_id "
        "WHERE d.sample_lab_no=? ORDER BY d.id", (lab_no,)).fetchall()
    return {"data": [dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# 阶段证据集与版本
# ---------------------------------------------------------------------------

def create_phase_set(payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    db = connection()
    _require_editor(db, payload["project_id"], actor_id)
    stamp = now()
    with transaction(immediate=True) as tx:
        cursor = tx.execute(
            "INSERT INTO phase_sets(project_id,name,status,created_by,created_at,updated_at) VALUES(?,?,'active',?,?,?)",
            (payload["project_id"], payload["name"], actor_id, stamp, stamp))
        set_id = cursor.lastrowid
        _audit(tx, "dating.phase_set.create", str(set_id), payload, project_id=payload["project_id"], actor_id=actor_id)
    return get_phase_set(set_id)


def _get_set(tx: sqlite3.Connection, set_id: int) -> sqlite3.Row:
    row = _row(tx, "SELECT * FROM phase_sets WHERE id=?", (set_id,))
    if row is None:
        raise DatingError("phase_set_not_found", "阶段证据集不存在", 404)
    return row


def create_phase_version(set_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    """更换曲线或增删成员样品都会生成可比较的新版本；已发布版本不可变。"""
    db = connection()
    phase_set = _get_set(db, set_id)
    _require_editor(db, phase_set["project_id"], actor_id)
    curve_version = payload.get("curve_version") or CURVE_VERSION
    try:
        curve_info(curve_version)
    except UnknownCurveError:
        raise DatingError("unknown_curve", "未内置的校准曲线版本", 404) from None
    members = payload["members"]
    if not members:
        raise DatingError("members_required", "阶段版本至少需要一个成员样品", 422)

    specs: list[dict[str, Any]] = []
    for spec in members:
        sample = _row(db, "SELECT * FROM dating_samples WHERE lab_no=?", (spec["sample_lab_no"].strip().upper(),))
        if sample is None:
            raise DatingError("sample_not_found", f"成员样品不存在：{spec['sample_lab_no']}", 404)
        if sample["project_id"] != phase_set["project_id"]:
            raise DatingError("sample_cross_project", f"成员样品 {sample['lab_no']} 不属于该证据集项目", 409)
        task_id = spec.get("calibration_task_id")
        if spec["included"]:
            if task_id is None:
                latest = _row(db, "SELECT id FROM calibration_tasks WHERE measurement_id IN "
                                  "(SELECT id FROM measurements WHERE sample_lab_no=?) AND status='completed' "
                                  "ORDER BY id DESC LIMIT 1", (sample["lab_no"],))
                if latest is None:
                    raise DatingError("no_completed_task", f"样品 {sample['lab_no']} 没有已完成的校准任务可供纳入", 422)
                task_id = latest["id"]
            task = _load_task(task_id)
            if task["status"] != "completed":
                raise DatingError("task_failed", f"样品 {sample['lab_no']} 的校准任务失败，不能纳入", 422)
            if task["measurement_id"] is not None:
                bound = _row(db, "SELECT 1 FROM measurements WHERE id=? AND sample_lab_no=?",
                             (task["measurement_id"], sample["lab_no"]))
                if bound is None:
                    raise DatingError("task_mismatch", f"校准任务 {task_id} 不属于样品 {sample['lab_no']}", 409)
            if task["curve_version"] != curve_version:
                raise DatingError(
                    "curve_mismatch",
                    f"样品 {sample['lab_no']} 的校准任务使用 {task['curve_version']}，与版本曲线 {curve_version} 不一致",
                    409,
                )
        elif not spec.get("exclusion_reason", "").strip():
            raise DatingError("exclusion_reason_required", f"排除样品 {sample['lab_no']} 必须给出排除理由", 422)
        specs.append({"lab_no": sample["lab_no"], "included": 1 if spec["included"] else 0,
                      "exclusion_reason": spec.get("exclusion_reason", ""), "calibration_task_id": task_id,
                      "note": spec.get("note", "")})

    member_digest = [
        {"lab_no": s["lab_no"], "included": s["included"], "exclusion_reason": s["exclusion_reason"],
         "calibration_task_id": s["calibration_task_id"]}
        for s in sorted(specs, key=lambda item: item["lab_no"])
    ]
    input_hash = request_hash({"curve_version": curve_version, "members": member_digest})

    stamp = now()
    with transaction(immediate=True) as tx:
        duplicate = _row(tx, "SELECT id FROM phase_versions WHERE set_id=? AND input_hash=?", (set_id, input_hash))
        if duplicate is not None:
            version_id = duplicate["id"]
        else:
            previous = _row(tx, "SELECT id,version_no FROM phase_versions WHERE set_id=? ORDER BY version_no DESC LIMIT 1", (set_id,))
            based_on = previous["id"] if previous else None
            version_no = (previous["version_no"] + 1) if previous else 1
            cursor = tx.execute(
                "INSERT INTO phase_versions(set_id,version_no,based_on_version_id,curve_version,input_hash,status,created_by,created_at) "
                "VALUES(?,?,?,?,?,'draft',?,?)",
                (set_id, version_no, based_on, curve_version, input_hash, actor_id, stamp))
            version_id = cursor.lastrowid
            for spec in specs:
                tx.execute(
                    "INSERT INTO phase_version_members(phase_version_id,sample_lab_no,calibration_task_id,included,exclusion_reason,note) "
                    "VALUES(?,?,?,?,?,?)",
                    (version_id, spec["lab_no"], spec["calibration_task_id"], spec["included"],
                     spec["exclusion_reason"], spec["note"]))
            _audit(tx, "dating.phase_version.create", str(version_id),
                   {"set_id": set_id, "version_no": version_no, "input_hash": input_hash},
                   project_id=phase_set["project_id"], actor_id=actor_id)
    return get_phase_version(version_id)


def _version_members(tx: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
    rows = tx.execute(
        "SELECT m.*, s.context_type,s.context_name,s.material,s.current_custodian,s.seal_state "
        "FROM phase_version_members m JOIN dating_samples s ON s.lab_no=m.sample_lab_no "
        "WHERE m.phase_version_id=? ORDER BY m.sample_lab_no", (version_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["lab_no"] = item["sample_lab_no"]
        risk_rows = tx.execute(
            "SELECT severity,status FROM pretreatment_risks WHERE sample_lab_no=?", (item["lab_no"],)).fetchall()
        item["open_risk_count"] = sum(1 for r in risk_rows if r["status"] == "open")
        item["high_risk_open"] = any(r["status"] == "open" and r["severity"] == "high" for r in risk_rows)
        detail = get_sample(item["lab_no"])
        item["chain_ok"] = detail["chain_ok"]
        item["publishable"] = detail["publishable"]
        if item["calibration_task_id"]:
            task = _load_task(item["calibration_task_id"])
            item["calibration"] = {"status": task["status"], "result": task["result"], "input_summary": task["input_summary"]}
        else:
            item["calibration"] = None
        result.append(item)
    return result


def _build_combined(version: sqlite3.Row, members: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    included_members = []
    for member in members:
        if member["included"] and member["calibration"] and member["calibration"]["status"] == "completed":
            included_members.append(member["calibration"]["result"])
    try:
        combined = combine_calibrated(
            [dict(result, included=True) for result in included_members],
            probability=DEFAULT_PROBABILITY,
            curve_version=version["curve_version"],
        )
        return combined, None
    except CalibrationError as exc:
        return None, {"code": exc.code, "message": exc.message}


def get_phase_version(version_id: int) -> dict[str, Any]:
    db = connection()
    version = _row(db, "SELECT * FROM phase_versions WHERE id=?", (version_id,))
    if version is None:
        raise DatingError("phase_version_not_found", "阶段版本不存在", 404)
    data = dict(version)
    members = _version_members(db, version_id)
    data["members"] = members
    combined = json.loads(version["combined_json"]) if version["combined_json"] else None
    if combined is None and version["status"] == "draft":
        combined, error = _build_combined(version, members)
        data["combined_preview"] = combined
        data["combined_error"] = error
    else:
        data["combined"] = combined.get("combined") if combined and "combined" in combined else combined
    data["set"] = dict(_row(db, "SELECT id,project_id,name,status FROM phase_sets WHERE id=?", (version["set_id"],)))
    return data


def publish_phase_version(version_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
    """发布后不可变；每个纳入成员在发布闸门处再次验证封签链路与风险标记。"""
    db = connection()
    version = _row(db, "SELECT * FROM phase_versions WHERE id=?", (version_id,))
    if version is None:
        raise DatingError("phase_version_not_found", "阶段版本不存在", 404)
    phase_set = _get_set(db, version["set_id"])
    role = _require_project_member(db, phase_set["project_id"], actor_id)
    if role not in {"owner", "researcher"}:
        raise DatingError("forbidden", "只有研究者可以发布阶段证据集", 403)
    if version["status"] == "published":
        raise DatingError("version_published", "该版本已发布，内容不可变", 409)

    members = _version_members(db, version_id)
    included = [m for m in members if m["included"]]
    if not included:
        raise DatingError("no_included_members", "没有纳入的成员样品，不能发布", 409)
    for member in included:
        sample = _row(db, "SELECT * FROM dating_samples WHERE lab_no=?", (member["lab_no"],))
        detail = get_sample(sample["lab_no"])
        if not detail["chain_ok"] or sample["seal_state"] != "intact":
            raise DatingError("seal_chain_broken", f"成员 {sample['lab_no']} 封签链路不连续或破损，不能发布", 409)
        if detail["high_risk_open"]:
            raise DatingError("open_high_risk", f"成员 {sample['lab_no']} 有未处置高风险标记，不能发布", 409)

    combined, error = _build_combined(version, members)
    if error is not None:
        raise DatingError(error["code"], f"组合分布计算失败：{error['message']}", 422)

    stamp = now()
    with transaction(immediate=True) as tx:
        tx.execute(
            "UPDATE phase_versions SET status='published',combined_json=?,published_by=?,published_at=? WHERE id=?",
            (stable_json({"combined": combined, "note": payload.get("note", ""), "published_at": stamp}),
             actor_id, stamp, version_id))
        _audit(tx, "dating.phase_version.publish", str(version_id),
               {"set_id": version["set_id"], "hpd": combined["hpd_intervals"]},
               project_id=phase_set["project_id"], actor_id=actor_id)
    return get_phase_version(version_id)


def get_phase_set(set_id: int) -> dict[str, Any]:
    db = connection()
    phase_set = _get_set(db, set_id)
    data = dict(phase_set)
    versions = db.execute(
        "SELECT id,version_no,based_on_version_id,curve_version,input_hash,status,created_by,created_at,published_at "
        "FROM phase_versions WHERE set_id=? ORDER BY version_no", (set_id,)).fetchall()
    data["versions"] = [dict(row) for row in versions]
    return data


def list_phase_sets(project_id: int | None = None) -> dict[str, Any]:
    db = connection()
    if project_id is None:
        rows = db.execute("SELECT * FROM phase_sets ORDER BY id").fetchall()
    else:
        rows = db.execute("SELECT * FROM phase_sets WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    return {"data": [dict(row) for row in rows]}


def compare_phase_versions(left_id: int, right_id: int) -> dict[str, Any]:
    """版本差异：曲线、成员集合、排除项与置信区间的可比较对照。"""
    left = get_phase_version(left_id)
    right = get_phase_version(right_id)
    if left["set_id"] != right["set_id"]:
        raise DatingError("set_mismatch", "只能比较同一阶段证据集的版本", 409)

    def snapshot(version: dict[str, Any]) -> dict[str, Any]:
        combined = version.get("combined")
        if combined is None:
            combined = (version.get("combined_preview") if version.get("combined_preview") else None)
        hpd = [{"start_cal_bp": i["start_cal_bp"], "end_cal_bp": i["end_cal_bp"], "probability": i["probability"]}
               for i in (combined["hpd_intervals"] if combined else [])]
        return {
            "version_id": version["id"],
            "version_no": version["version_no"],
            "status": version["status"],
            "curve_version": version["curve_version"],
            "included": sorted(m["lab_no"] for m in version["members"] if m["included"]),
            "excluded": {m["lab_no"]: m["exclusion_reason"] for m in version["members"] if not m["included"]},
            "hpd_intervals": hpd,
            "summary": combined["summary"] if combined else None,
        }

    a, b = snapshot(left), snapshot(right)
    added = sorted(set(b["included"]) - set(a["included"]))
    removed = sorted(set(a["included"]) - set(b["included"]))
    curve_changed = a["curve_version"] != b["curve_version"]
    return {
        "set_id": left["set_id"],
        "curve_changed": curve_changed,
        "members_added": added,
        "members_removed": removed,
        "exclusions_changed": a["excluded"] != b["excluded"],
        "hpd_changed": a["hpd_intervals"] != b["hpd_intervals"],
        "left": a,
        "right": b,
    }
