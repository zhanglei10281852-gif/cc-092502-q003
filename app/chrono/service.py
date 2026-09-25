"""年代证据领域服务。

样品身份连续性由“实验室编号 + 封签事件序列”保证：
registered → sealed → in_transit → received → opened，
任何破损都会进入 quarantined，必须 reseal 并重新走完交接才能打开。
前处理、测值、校准、发布都挂在这条链路上。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.chrono.curves import get_curve
from app.chrono.engine import ALGORITHM_NAME, ALGORITHM_VERSION, CalibrationError, EngineInput, calibrate
from app.database import connection, now, transaction
from app.security import request_hash, sanitize, stable_json

# 封签事件 -> 允许的前置 chain_status
SEAL_TRANSITIONS: dict[str, set[str]] = {
    "seal": {"registered"},
    "reseal": {"quarantined"},
    "handover": {"sealed"},
    "receive": {"in_transit"},
    "open": {"received"},
    "damage": {"sealed", "in_transit", "received", "opened", "quarantined"},
}


class ChronoError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class ChronoService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()

    # ------------------------------------------------------------------ audit
    def audit(self, action: str, resource_id: str, payload: dict[str, Any], *, project_id: int | None = None, actor_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit_events(project_id,actor_id,action,resource_type,resource_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, actor_id, action, "chrono", resource_id, stable_json(sanitize(payload)), now()),
        )

    def require_project_role(self, project_id: int | None, user_id: int, allowed: set[str]) -> None:
        if not project_id:
            return
        row = self.db.execute("SELECT role FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id)).fetchone()
        if row is None or row["role"] not in allowed:
            raise ChronoError("forbidden", "当前用户没有该项目的年代证据操作权限", 403)

    def _next_no(self, counter: str, prefix: str) -> str:
        row = self.db.execute(
            "INSERT INTO counters(name,value) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1 RETURNING value",
            (counter,),
        ).fetchone()
        return f"{prefix}-{row[0]:06d}"

    # ---------------------------------------------------------------- samples
    def register_sample(self, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_project_role(payload.get("project_id"), actor_id, {"owner", "researcher", "recorder"})
        stamp = now()
        with transaction(immediate=True) as db:
            lab_no = self._next_no("sample", "LAB")
            cursor = db.execute(
                "INSERT INTO dating_samples(lab_no,project_id,field_code,context_type,context_name,material,note,chain_status,registered_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (lab_no, payload.get("project_id"), payload["field_code"], payload["context_type"], payload["context_name"], payload["material"], payload.get("note", ""), "registered", actor_id, stamp, stamp),
            )
            sample_id = cursor.lastrowid
            self.audit("chrono.sample.register", lab_no, payload, project_id=payload.get("project_id"), actor_id=actor_id)
            return self._sample_view(db, sample_id)

    def _get_sample(self, db: sqlite3.Connection, lab_no: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM dating_samples WHERE lab_no=?", (lab_no,)).fetchone()
        if row is None:
            raise ChronoError("sample_not_found", f"实验室编号 {lab_no} 不存在", 404)
        return row

    def add_seal_event(self, lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        event_type = payload["event_type"]
        with transaction(immediate=True) as db:
            sample = self._get_sample(db, lab_no)
            current = sample["chain_status"]
            if current not in SEAL_TRANSITIONS[event_type]:
                raise ChronoError(
                    "chain_transition_denied",
                    f"封签事件 {event_type} 不能从状态 {current} 发起：样品交接链路不可跳过",
                    409,
                )
            intact = 1 if payload.get("seal_intact", True) else 0
            if event_type in ("seal", "reseal"):
                if not payload.get("seal_id"):
                    raise ChronoError("seal_id_required", "封签必须登记封签编号", 422)
                intact = 1
            if event_type == "handover":
                if not payload.get("from_party") or not payload.get("to_party"):
                    raise ChronoError("handover_parties_required", "交接必须记录交出方与接收方", 422)
                intact = 1
            if event_type == "damage":
                intact = 0
            seq_row = db.execute("SELECT COALESCE(MAX(seq),0)+1 AS next FROM seal_events WHERE sample_id=?", (sample["id"],)).fetchone()
            db.execute(
                "INSERT INTO seal_events(sample_id,seq,event_type,seal_id,from_party,to_party,actor,seal_intact,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sample["id"], seq_row["next"], event_type, payload.get("seal_id", ""), payload.get("from_party", ""), payload.get("to_party", ""), payload.get("actor", ""), intact, payload.get("note", ""), actor_id, now()),
            )
            new_status = self._next_chain_status(event_type, intact, current)
            db.execute("UPDATE dating_samples SET chain_status=?,updated_at=? WHERE id=?", (new_status, now(), sample["id"]))
            self.audit("chrono.seal.event", f"{lab_no}#{seq_row['next']}", {"event_type": event_type, "seal_intact": bool(intact), **payload}, project_id=sample["project_id"], actor_id=actor_id)
            return self._sample_view(db, sample["id"])

    @staticmethod
    def _next_chain_status(event_type: str, intact: int, current: str) -> str:
        if event_type == "damage" or (event_type == "receive" and not intact):
            return "quarantined"
        return {
            "seal": "sealed",
            "reseal": "sealed",
            "handover": "in_transit",
            "receive": "received",
            "open": "opened",
        }[event_type]

    def add_pretreatment(self, lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            sample = self._get_sample(db, lab_no)
            if sample["chain_status"] != "opened":
                raise ChronoError("sample_not_opened", "前处理只能在样品按链路签收并开封后登记", 409)
            risks = sorted(str(item) for item in payload.get("risks", []))
            cursor = db.execute(
                "INSERT INTO pretreatment_records(sample_id,method,operator,conclusion,risks_json,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (sample["id"], payload["method"], payload["operator"], payload["conclusion"], stable_json(risks), payload.get("note", ""), actor_id, now()),
            )
            db.execute("UPDATE dating_samples SET updated_at=? WHERE id=?", (now(), sample["id"]))
            self.audit("chrono.pretreatment.add", f"{lab_no}#{cursor.lastrowid}", payload, project_id=sample["project_id"], actor_id=actor_id)
            return self._sample_view(db, sample["id"])

    def add_measurement(self, lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            sample = self._get_sample(db, lab_no)
            if sample["chain_status"] != "opened":
                raise ChronoError("sample_not_opened", "测值只能在样品按链路签收并开封后登记", 409)
            latest = db.execute("SELECT conclusion FROM pretreatment_records WHERE sample_id=? ORDER BY id DESC LIMIT 1", (sample["id"],)).fetchone()
            if latest is None:
                raise ChronoError("pretreatment_missing", "登记实验测值前必须先有前处理记录", 409)
            if latest["conclusion"] == "fail":
                raise ChronoError("pretreatment_failed", "最近一次前处理结论为失败，不能登记测值；请重新前处理并通过后再测", 409)
            stamp = now()
            measurement_no = payload.get("measurement_no") or self._next_no("measurement", "MEAS")
            try:
                db.execute(
                    "INSERT INTO measurements(measurement_no,sample_id,c14_age_bp,c14_error_bp,delta_c13,instrument,operator,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (measurement_no, sample["id"], payload["c14_age_bp"], payload["c14_error_bp"], payload.get("delta_c13"), payload.get("instrument", ""), payload.get("operator", ""), actor_id, stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ChronoError("measurement_exists", "测值编号已存在", 409) from exc
            db.execute("UPDATE dating_samples SET updated_at=? WHERE id=?", (stamp, sample["id"]))
            self.audit("chrono.measurement.add", measurement_no, {"lab_no": lab_no, **payload}, project_id=sample["project_id"], actor_id=actor_id)
            return self._sample_view(db, sample["id"])

    # ----------------------------------------------------------- calibration
    def submit_calibration(self, lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        curve_name = payload["curve_name"]
        try:
            curve = get_curve(curve_name)
        except KeyError:
            raise ChronoError("curve_not_found", f"未知校准曲线: {curve_name}", 404)
        with transaction(immediate=True) as db:
            sample = self._get_sample(db, lab_no)
            measurement = db.execute("SELECT * FROM measurements WHERE measurement_no=?", (payload["measurement_no"],)).fetchone()
            if measurement is None:
                raise ChronoError("measurement_not_found", f"测值 {payload['measurement_no']} 不存在", 404)
            if measurement["sample_id"] != sample["id"]:
                raise ChronoError("measurement_sample_mismatch", "该测值不属于路径指定的样品", 409)

            digest_input = {
                "measurement_no": measurement["measurement_no"],
                "c14_age_bp": measurement["c14_age_bp"],
                "c14_error_bp": measurement["c14_error_bp"],
                "delta_c13": measurement["delta_c13"],
                "curve_name": curve.name,
                "curve_checksum": curve.summary()["checksum_sha256"],
                "reservoir_offset_bp": payload.get("reservoir_offset_bp", 0.0),
                "reservoir_error_bp": payload.get("reservoir_error_bp", 0.0),
                "algorithm": f"{ALGORITHM_NAME}@{ALGORITHM_VERSION}",
            }
            input_hash = request_hash(digest_input)
            existing = db.execute("SELECT * FROM calibration_jobs WHERE input_hash=?", (input_hash,)).fetchone()
            if existing is not None:
                return self._calibration_view(db, existing)

            task_no = self._next_no("calibration", "CAL")
            stamp = now()
            input_summary = {
                "measurement": {
                    "measurement_no": measurement["measurement_no"],
                    "c14_age_bp": measurement["c14_age_bp"],
                    "c14_error_bp": measurement["c14_error_bp"],
                    "delta_c13": measurement["delta_c13"],
                },
                "curve": curve.summary(),
                "reservoir_offset_bp": payload.get("reservoir_offset_bp", 0.0),
                "reservoir_error_bp": payload.get("reservoir_error_bp", 0.0),
                "algorithm": {"name": ALGORITHM_NAME, "version": ALGORITHM_VERSION},
            }
            status, result_json, error_text = "done", "{}", ""
            try:
                result = calibrate(
                    EngineInput(
                        c14_age_bp=float(measurement["c14_age_bp"]),
                        c14_error_bp=float(measurement["c14_error_bp"]),
                        curve=curve,
                        reservoir_offset_bp=float(payload.get("reservoir_offset_bp", 0.0)),
                        reservoir_error_bp=float(payload.get("reservoir_error_bp", 0.0)),
                    )
                )
                result_json = stable_json(result)
            except CalibrationError as exc:
                status, error_text = "failed", str(exc)

            cursor = db.execute(
                "INSERT INTO calibration_jobs(task_no,measurement_id,curve_name,curve_version,curve_checksum,reservoir_offset_bp,reservoir_error_bp,input_hash,input_summary_json,status,result_json,error_text,attempts,algorithm_name,algorithm_version,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_no, measurement["id"], curve.name, curve.version, curve.summary()["checksum_sha256"], payload.get("reservoir_offset_bp", 0.0), payload.get("reservoir_error_bp", 0.0), input_hash, stable_json(input_summary), status, result_json, error_text, 1, ALGORITHM_NAME, ALGORITHM_VERSION, actor_id, stamp, stamp),
            )
            self.audit("chrono.calibration.submit", task_no, {"lab_no": lab_no, "status": status, "curve": curve.name, "input_hash": input_hash}, project_id=sample["project_id"], actor_id=actor_id)
            row = db.execute("SELECT * FROM calibration_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._calibration_view(db, row)

    def get_calibration(self, task_no: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM calibration_jobs WHERE task_no=?", (task_no,)).fetchone()
        if row is None:
            raise ChronoError("calibration_not_found", f"校准任务 {task_no} 不存在", 404)
        return self._calibration_view(self.db, row)

    # ------------------------------------------------------------ publication
    def publish_decision(self, lab_no: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            sample = self._get_sample(db, lab_no)
            task = db.execute("SELECT * FROM calibration_jobs WHERE task_no=?", (payload["task_no"],)).fetchone()
            if task is None:
                raise ChronoError("calibration_not_found", f"校准任务 {payload['task_no']} 不存在", 404)
            measurement = db.execute("SELECT * FROM measurements WHERE id=?", (task["measurement_id"],)).fetchone()
            if measurement["sample_id"] != sample["id"]:
                raise ChronoError("calibration_sample_mismatch", "校准任务不属于该样品", 409)
            if db.execute("SELECT 1 FROM publications WHERE sample_id=?", (sample["id"],)).fetchone():
                raise ChronoError("already_published", "该样品已有不可变的发布决定", 409)
            # 链路守卫：必须走完 seal→handover→receive→open；破损后未重新封签交接无法到达 opened
            if sample["chain_status"] != "opened":
                raise ChronoError("chain_incomplete", f"样品链路状态为 {sample['chain_status']}，封签破损或交接未完成时禁止发布结果", 409)
            latest_pt = db.execute("SELECT conclusion FROM pretreatment_records WHERE sample_id=? ORDER BY id DESC LIMIT 1", (sample["id"],)).fetchone()
            if latest_pt is not None and latest_pt["conclusion"] == "fail":
                raise ChronoError("pretreatment_failed", "前处理失败的样品禁止发布结果", 409)
            if task["status"] != "done":
                raise ChronoError("calibration_failed", "校准失败的任务不能发布；请更换曲线或核对测值后重新提交", 409)
            result = self._load_result(task)
            if payload["decision"] == "adopted" and result.get("truncated_at_curve_boundary") and not payload.get("boundary_acknowledged"):
                raise ChronoError("boundary_ack_required", "概率分布被校准曲线边界截断，采纳前必须显式确认边界风险", 422)
            stamp = now()
            db.execute(
                "INSERT INTO publications(sample_id,calibration_job_id,decision,reason,boundary_acknowledged,published_by,published_at) VALUES(?,?,?,?,?,?,?)",
                (sample["id"], task["id"], payload["decision"], payload.get("reason", ""), 1 if payload.get("boundary_acknowledged") else 0, actor_id, stamp),
            )
            self.audit("chrono.publish", lab_no, payload, project_id=sample["project_id"], actor_id=actor_id)
            return self._sample_view(db, sample["id"])

    # ------------------------------------------------------------- phase sets
    def create_phase_set(self, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_project_role(payload.get("project_id"), actor_id, {"owner", "researcher"})
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO phase_sets(phase_code,name,project_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (payload["phase_code"], payload["name"], payload.get("project_id"), actor_id, stamp, stamp),
                )
                self.audit("chrono.phase.create", payload["phase_code"], payload, project_id=payload.get("project_id"), actor_id=actor_id)
                return self._phase_view(db, cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ChronoError("phase_exists", "阶段证据集编码已存在", 409) from exc

    def create_phase_version(self, phase_code: str, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        try:
            curve = get_curve(payload["curve_name"])
        except KeyError:
            raise ChronoError("curve_not_found", f"未知校准曲线: {payload['curve_name']}", 404)
        members_in = sorted(payload["members"], key=lambda m: m["sample_id"])  # 稳定排序
        with transaction(immediate=True) as db:
            phase = db.execute("SELECT * FROM phase_sets WHERE phase_code=?", (phase_code,)).fetchone()
            if phase is None:
                raise ChronoError("phase_not_found", f"阶段证据集 {phase_code} 不存在", 404)
            self.require_project_role(phase["project_id"], actor_id, {"owner", "researcher"})

            resolved: list[dict[str, Any]] = []
            seen: set[int] = set()
            for spec in members_in:
                sid = spec["sample_id"]
                if sid in seen:
                    raise ChronoError("duplicate_member", f"样品 {sid} 在成员列表中重复", 422)
                seen.add(sid)
                sample = db.execute("SELECT * FROM dating_samples WHERE id=?", (sid,)).fetchone()
                if sample is None:
                    raise ChronoError("sample_not_found", f"样品 {sid} 不存在", 404)
                job = db.execute(
                    "SELECT * FROM calibration_jobs j JOIN measurements m ON m.id=j.measurement_id "
                    "WHERE m.sample_id=? AND j.curve_name=? AND j.status='done' ORDER BY j.id DESC LIMIT 1",
                    (sid, curve.name),
                ).fetchone()
                if job is None:
                    raise ChronoError("member_not_calibrated", f"样品 {sample['lab_no']} 缺少曲线 {curve.name} 上成功的校准结果", 422)
                pub = db.execute("SELECT * FROM publications WHERE sample_id=?", (sid,)).fetchone()
                if pub is None:
                    raise ChronoError("member_not_published", f"样品 {sample['lab_no']} 尚无发布决定，不能进入阶段证据集", 422)
                if not spec["excluded"] and pub["decision"] != "adopted":
                    raise ChronoError("member_rejected", f"样品 {sample['lab_no']} 的决定为 rejected；如仍要纳入请标记 excluded 并给出理由", 422)
                if spec["excluded"] and not spec["exclude_reason"].strip():
                    raise ChronoError("exclude_reason_required", f"排除样品 {sample['lab_no']} 必须给出排除理由", 422)
                resolved.append({"spec": spec, "sample": sample, "job": job, "publication": pub})

            digest = {
                "curve_name": curve.name,
                "curve_checksum": curve.summary()["checksum_sha256"],
                "members": [
                    {
                        "sample_id": item["sample"]["id"],
                        "calibration_job_id": item["job"]["id"],
                        "excluded": item["spec"]["excluded"],
                        "exclude_reason": item["spec"]["exclude_reason"],
                    }
                    for item in resolved
                ],
            }
            input_hash = request_hash(digest)
            same = db.execute("SELECT id FROM phase_versions WHERE phase_set_id=? AND input_hash=?", (phase["id"], input_hash)).fetchone()
            if same is not None:
                return self._version_view(db, same["id"])

            version_no = db.execute("SELECT COALESCE(MAX(version_no),0)+1 AS next FROM phase_versions WHERE phase_set_id=?", (phase["id"],)).fetchone()["next"]
            stamp = now()
            cursor = db.execute(
                "INSERT INTO phase_versions(phase_set_id,version_no,curve_name,input_hash,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (phase["id"], version_no, curve.name, input_hash, actor_id, stamp),
            )
            version_id = cursor.lastrowid
            for position, item in enumerate(resolved):
                spec = item["spec"]
                db.execute(
                    "INSERT INTO phase_members(phase_version_id,position,sample_id,calibration_job_id,excluded,exclude_reason) VALUES(?,?,?,?,?,?)",
                    (version_id, position, item["sample"]["id"], item["job"]["id"], 1 if spec["excluded"] else 0, spec["exclude_reason"]),
                )
            db.execute("UPDATE phase_sets SET updated_at=? WHERE id=?", (stamp, phase["id"]))
            self.audit("chrono.phase.version_create", f"{phase_code}#v{version_no}", {"curve": curve.name, "members": digest["members"], "note": payload.get("note", "")}, project_id=phase["project_id"], actor_id=actor_id)
            return self._version_view(db, version_id)

    def publish_phase_version(self, phase_code: str, version_no: int, actor_id: int) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            version = self._get_version(db, phase_code, version_no)
            phase = db.execute("SELECT * FROM phase_sets WHERE id=?", (version["phase_set_id"],)).fetchone()
            self.require_project_role(phase["project_id"], actor_id, {"owner", "researcher"})
            if version["status"] == "published":
                return self._version_view(db, version["id"])
            stamp = now()
            db.execute("UPDATE phase_versions SET status='published',published_by=?,published_at=? WHERE id=?", (actor_id, stamp, version["id"]))
            self.audit("chrono.phase.version_publish", f"{phase_code}#v{version_no}", {"version_no": version_no}, project_id=phase["project_id"], actor_id=actor_id)
            return self._version_view(db, version["id"])

    def get_phase_set(self, phase_code: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM phase_sets WHERE phase_code=?", (phase_code,)).fetchone()
        if row is None:
            raise ChronoError("phase_not_found", f"阶段证据集 {phase_code} 不存在", 404)
        return self._phase_view(self.db, row["id"])

    def get_version(self, phase_code: str, version_no: int) -> dict[str, Any]:
        return self._version_view(self.db, self._get_version(self.db, phase_code, version_no)["id"])

    def diff_versions(self, phase_code: str, from_version: int, to_version: int) -> dict[str, Any]:
        db = self.db
        old = self._get_version(db, phase_code, from_version)
        new = self._get_version(db, phase_code, to_version)
        old_members = {row["sample_id"]: row for row in db.execute("SELECT * FROM phase_members WHERE phase_version_id=?", (old["id"],)).fetchall()}
        new_members = {row["sample_id"]: row for row in db.execute("SELECT * FROM phase_members WHERE phase_version_id=?", (new["id"],)).fetchall()}
        old_ids, new_ids = set(old_members), set(new_members)

        def member_brief(member_row: sqlite3.Row) -> dict[str, Any]:
            sample = db.execute("SELECT lab_no,field_code,context_name,context_type,material FROM dating_samples WHERE id=?", (member_row["sample_id"],)).fetchone()
            job = db.execute("SELECT * FROM calibration_jobs WHERE id=?", (member_row["calibration_job_id"],)).fetchone()
            result = self._load_result(job)
            return {
                "sample_id": member_row["sample_id"],
                "lab_no": sample["lab_no"],
                "field_code": sample["field_code"],
                "context_name": sample["context_name"],
                "excluded": bool(member_row["excluded"]),
                "exclude_reason": member_row["exclude_reason"],
                "curve_name": job["curve_name"],
                "task_no": job["task_no"],
                "hpd95": self._compact_intervals(result.get("hpd95", [])),
                "mode": result.get("mode"),
            }

        changed: list[dict[str, Any]] = []
        for sample_id in sorted(old_ids & new_ids):
            a, b = old_members[sample_id], new_members[sample_id]
            changes: dict[str, Any] = {}
            if a["excluded"] != b["excluded"] or a["exclude_reason"] != b["exclude_reason"]:
                changes["exclusion"] = {
                    "from": {"excluded": bool(a["excluded"]), "exclude_reason": a["exclude_reason"]},
                    "to": {"excluded": bool(b["excluded"]), "exclude_reason": b["exclude_reason"]},
                }
            if a["calibration_job_id"] != b["calibration_job_id"]:
                ja = db.execute("SELECT curve_name,task_no FROM calibration_jobs WHERE id=?", (a["calibration_job_id"],)).fetchone()
                jb = db.execute("SELECT curve_name,task_no FROM calibration_jobs WHERE id=?", (b["calibration_job_id"],)).fetchone()
                changes["calibration"] = {"from": dict(ja), "to": dict(jb)}
            if changes:
                changes["sample_id"] = sample_id
                changed.append(changes)

        return {
            "phase_code": phase_code,
            "curve_changed": old["curve_name"] != new["curve_name"],
            "curve": {"from": old["curve_name"], "to": new["curve_name"]},
            "added": [member_brief(new_members[i]) for i in sorted(new_ids - old_ids)],
            "removed": [member_brief(old_members[i]) for i in sorted(old_ids - new_ids)],
            "changed": changed,
            "versions": {"from": from_version, "to": to_version},
        }

    def list_samples(self, project_id: int | None = None, context_type: str | None = None) -> dict[str, Any]:
        sql = "SELECT id FROM dating_samples"
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        if context_type:
            clauses.append("context_type=?")
            params.append(context_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY lab_no"
        rows = self.db.execute(sql, params).fetchall()
        return {"data": [self._sample_view(self.db, row["id"]) for row in rows]}

    def get_sample(self, lab_no: str) -> dict[str, Any]:
        sample = self._get_sample(self.db, lab_no)
        return self._sample_view(self.db, sample["id"])

    # ------------------------------------------------------------- serializers
    @staticmethod
    def _load_result(task: sqlite3.Row) -> dict[str, Any]:
        import json

        return json.loads(task["result_json"]) if task["result_json"] else {}

    def _calibration_view(self, db: sqlite3.Connection, task: sqlite3.Row) -> dict[str, Any]:
        import json

        result = self._load_result(task)
        measurement = db.execute("SELECT * FROM measurements WHERE id=?", (task["measurement_id"],)).fetchone()
        sample = db.execute("SELECT * FROM dating_samples WHERE id=?", (measurement["sample_id"],)).fetchone()
        view = {
            "task_no": task["task_no"],
            "lab_no": sample["lab_no"],
            "status": task["status"],
            "error": task["error_text"],
            "attempts": task["attempts"],
            "curve": {"name": task["curve_name"], "version": task["curve_version"], "checksum_sha256": task["curve_checksum"]},
            "reservoir_offset_bp": task["reservoir_offset_bp"],
            "reservoir_error_bp": task["reservoir_error_bp"],
            "input_summary": json.loads(task["input_summary_json"]),
            "input_hash": task["input_hash"],
            "algorithm": {"name": task["algorithm_name"], "version": task["algorithm_version"]},
            "created_at": task["created_at"],
        }
        if task["status"] == "done":
            view.update(
                {
                    "mode": result["mode"],
                    "hpd68": result["hpd68"],
                    "hpd95": result["hpd95"],
                    "is_multimodal": result["is_multimodal"],
                    "truncated_at_curve_boundary": result["truncated_at_curve_boundary"],
                    "boundary_warnings": result["boundary_warnings"],
                    "posterior": result["posterior"],
                }
            )
        return view

    def _risk_flags(self, db: sqlite3.Connection, sample: sqlite3.Row) -> list[str]:
        flags: list[str] = []
        last_damage = db.execute("SELECT MAX(seq) AS s FROM seal_events WHERE sample_id=? AND event_type='damage'", (sample["id"],)).fetchone()["s"]
        if last_damage is not None:
            last_reseal = db.execute("SELECT MAX(seq) AS s FROM seal_events WHERE sample_id=? AND event_type='reseal'", (sample["id"],)).fetchone()["s"]
            flags.append("seal_damaged_recovered" if last_reseal is not None and last_reseal > last_damage else "seal_damaged_unresolved")
        latest_pt = db.execute("SELECT conclusion FROM pretreatment_records WHERE sample_id=? ORDER BY id DESC LIMIT 1", (sample["id"],)).fetchone()
        if latest_pt is not None and latest_pt["conclusion"] in ("caution", "fail"):
            flags.append(f"pretreatment_{latest_pt['conclusion']}")
        job = db.execute(
            "SELECT j.* FROM calibration_jobs j JOIN measurements m ON m.id=j.measurement_id "
            "WHERE m.sample_id=? AND j.status='done' ORDER BY j.id DESC LIMIT 1",
            (sample["id"],),
        ).fetchone()
        if job is not None and self._load_result(job).get("truncated_at_curve_boundary"):
            flags.append("calibration_boundary_truncated")
        pub = db.execute("SELECT decision FROM publications WHERE sample_id=?", (sample["id"],)).fetchone()
        if pub is not None and pub["decision"] == "rejected":
            flags.append("date_rejected")
        return flags

    def _sample_view(self, db: sqlite3.Connection, sample_id: int) -> dict[str, Any]:
        import json

        sample = db.execute("SELECT * FROM dating_samples WHERE id=?", (sample_id,)).fetchone()
        data = {
            "lab_no": sample["lab_no"],
            "sample_id": sample["id"],
            "project_id": sample["project_id"],
            "field_code": sample["field_code"],
            "source": {
                "context_type": sample["context_type"],
                "context_name": sample["context_name"],
                "material": sample["material"],
            },
            "note": sample["note"],
            "chain_status": sample["chain_status"],
            "registered_at": sample["created_at"],
            "seal_events": [
                {
                    "seq": row["seq"],
                    "event_type": row["event_type"],
                    "seal_id": row["seal_id"],
                    "from_party": row["from_party"],
                    "to_party": row["to_party"],
                    "actor": row["actor"],
                    "seal_intact": bool(row["seal_intact"]),
                    "note": row["note"],
                    "at": row["created_at"],
                }
                for row in db.execute("SELECT * FROM seal_events WHERE sample_id=? ORDER BY seq", (sample_id,)).fetchall()
            ],
            "pretreatments": [
                {
                    "method": row["method"],
                    "operator": row["operator"],
                    "conclusion": row["conclusion"],
                    "risks": json.loads(row["risks_json"]),
                    "note": row["note"],
                    "at": row["created_at"],
                }
                for row in db.execute("SELECT * FROM pretreatment_records WHERE sample_id=? ORDER BY id", (sample_id,)).fetchall()
            ],
            "measurements": [
                {
                    "measurement_no": row["measurement_no"],
                    "c14_age_bp": row["c14_age_bp"],
                    "c14_error_bp": row["c14_error_bp"],
                    "delta_c13": row["delta_c13"],
                    "instrument": row["instrument"],
                    "operator": row["operator"],
                    "at": row["created_at"],
                }
                for row in db.execute("SELECT * FROM measurements WHERE sample_id=? ORDER BY id", (sample_id,)).fetchall()
            ],
            "risk_flags": self._risk_flags(db, sample),
        }
        jobs = db.execute(
            "SELECT j.* FROM calibration_jobs j JOIN measurements m ON m.id=j.measurement_id WHERE m.sample_id=? ORDER BY j.id",
            (sample_id,),
        ).fetchall()
        data["calibrations"] = [self._calibration_view(db, job) for job in jobs]
        pub = db.execute("SELECT * FROM publications WHERE sample_id=?", (sample_id,)).fetchone()
        if pub is not None:
            data["publication"] = {
                "task_no": db.execute("SELECT task_no FROM calibration_jobs WHERE id=?", (pub["calibration_job_id"],)).fetchone()["task_no"],
                "decision": pub["decision"],
                "reason": pub["reason"],
                "boundary_acknowledged": bool(pub["boundary_acknowledged"]),
                "published_at": pub["published_at"],
                "immutable": True,
            }
        return data

    def _get_version(self, db: sqlite3.Connection, phase_code: str, version_no: int) -> sqlite3.Row:
        row = db.execute(
            "SELECT pv.* FROM phase_versions pv JOIN phase_sets ps ON ps.id=pv.phase_set_id WHERE ps.phase_code=? AND pv.version_no=?",
            (phase_code, version_no),
        ).fetchone()
        if row is None:
            raise ChronoError("version_not_found", f"{phase_code} 的 v{version_no} 不存在", 404)
        return row

    def _compact_intervals(self, intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "start_cal_bp": item["start_cal_bp"],
                "end_cal_bp": item["end_cal_bp"],
                "start_label": item["start"]["cal_bce_ce"],
                "end_label": item["end"]["cal_bce_ce"],
                "probability": item["probability"],
            }
            for item in intervals
        ]

    def _version_view(self, db: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        import json

        version = db.execute("SELECT * FROM phase_versions WHERE id=?", (version_id,)).fetchone()
        phase = db.execute("SELECT * FROM phase_sets WHERE id=?", (version["phase_set_id"],)).fetchone()
        members = []
        for row in db.execute("SELECT * FROM phase_members WHERE phase_version_id=? ORDER BY position,sample_id", (version_id,)).fetchall():
            sample = db.execute("SELECT * FROM dating_samples WHERE id=?", (row["sample_id"],)).fetchone()
            job = db.execute("SELECT * FROM calibration_jobs WHERE id=?", (row["calibration_job_id"],)).fetchone()
            result = self._load_result(job)
            pub = db.execute("SELECT decision,reason FROM publications WHERE sample_id=?", (sample["id"],)).fetchone()
            members.append(
                {
                    "position": row["position"],
                    "sample_id": sample["id"],
                    "lab_no": sample["lab_no"],
                    "field_code": sample["field_code"],
                    "source": {"context_type": sample["context_type"], "context_name": sample["context_name"], "material": sample["material"]},
                    "excluded": bool(row["excluded"]),
                    "exclude_reason": row["exclude_reason"],
                    "decision": pub["decision"] if pub else None,
                    "risk_flags": self._risk_flags(db, sample),
                    "calibration": {
                        "task_no": job["task_no"],
                        "curve_name": job["curve_name"],
                        "curve_version": job["curve_version"],
                        "mode": result.get("mode"),
                        "hpd68": self._compact_intervals(result.get("hpd68", [])),
                        "hpd95": self._compact_intervals(result.get("hpd95", [])),
                        "is_multimodal": result.get("is_multimodal"),
                        "truncated_at_curve_boundary": result.get("truncated_at_curve_boundary"),
                    },
                }
            )
        included = [m for m in members if not m["excluded"]]
        return {
            "phase_code": phase["phase_code"],
            "phase_name": phase["name"],
            "version_no": version["version_no"],
            "status": version["status"],
            "curve_name": version["curve_name"],
            "input_hash": version["input_hash"],
            "created_at": version["created_at"],
            "published_at": version["published_at"] or None,
            "members": members,
            "included_count": len(included),
            "excluded_count": len(members) - len(included),
            "immutable": version["status"] == "published",
        }

    def _phase_view(self, db: sqlite3.Connection, phase_set_id: int) -> dict[str, Any]:
        phase = db.execute("SELECT * FROM phase_sets WHERE id=?", (phase_set_id,)).fetchone()
        versions = [
            {
                "version_no": row["version_no"],
                "status": row["status"],
                "curve_name": row["curve_name"],
                "input_hash": row["input_hash"],
                "created_at": row["created_at"],
                "published_at": row["published_at"] or None,
            }
            for row in db.execute("SELECT * FROM phase_versions WHERE phase_set_id=? ORDER BY version_no", (phase_set_id,)).fetchall()
        ]
        return {
            "phase_code": phase["phase_code"],
            "name": phase["name"],
            "project_id": phase["project_id"],
            "created_at": phase["created_at"],
            "versions": versions,
        }
