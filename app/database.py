from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import settings

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 code TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL,
 site_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed','archived')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 username TEXT NOT NULL UNIQUE,
 display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('owner','researcher','recorder','reviewer','viewer')),
 joined_at TEXT NOT NULL,
 PRIMARY KEY(project_id,user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE,
 expires_at TEXT NOT NULL,
 revoked_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL,
 resource_type TEXT NOT NULL,
 resource_id TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_records (
 scope TEXT NOT NULL,
 request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 response_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(scope,request_key)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
 job_type TEXT NOT NULL,
 job_key TEXT NOT NULL UNIQUE,
 input_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','retry','done','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT NOT NULL DEFAULT '',
 lease_until TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,created_at,id);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events(project_id,created_at,id);

-- 年代证据服务：取样登记、封签交接、前处理风险、原始测值、校准任务、采用决定、阶段证据集
CREATE TABLE IF NOT EXISTS dating_samples (
 lab_no TEXT PRIMARY KEY,
 project_id INTEGER NOT NULL REFERENCES projects(id),
 context_type TEXT NOT NULL CHECK(context_type IN ('stratum','pit')),
 context_name TEXT NOT NULL,
 material TEXT NOT NULL,
 sample_note TEXT NOT NULL DEFAULT '',
 collected_at TEXT NOT NULL,
 current_custodian TEXT NOT NULL,
 seal_state TEXT NOT NULL DEFAULT 'intact' CHECK(seal_state IN ('intact','broken')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
 registered_by INTEGER NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seal_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_lab_no TEXT NOT NULL REFERENCES dating_samples(lab_no),
 seq INTEGER NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN ('seal','transfer','break','reseal')),
 from_party TEXT NOT NULL DEFAULT '',
 to_party TEXT NOT NULL DEFAULT '',
 seal_state TEXT NOT NULL CHECK(seal_state IN ('intact','broken')),
 note TEXT NOT NULL DEFAULT '',
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(sample_lab_no,seq)
);
DROP TRIGGER IF EXISTS trg_seal_events_no_update;
CREATE TRIGGER trg_seal_events_no_update BEFORE UPDATE ON seal_events
BEGIN
 SELECT RAISE(ABORT,'封签事件不可改写：seal_events 为只追加表');
END;
DROP TRIGGER IF EXISTS trg_seal_events_no_delete;
CREATE TRIGGER trg_seal_events_no_delete BEFORE DELETE ON seal_events
BEGIN
 SELECT RAISE(ABORT,'封签事件不可删除：样品身份链路必须完整保留');
END;
CREATE TABLE IF NOT EXISTS pretreatment_risks (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_lab_no TEXT NOT NULL REFERENCES dating_samples(lab_no),
 risk_code TEXT NOT NULL,
 severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
 note TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
 resolution_note TEXT NOT NULL DEFAULT '',
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 resolved_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS measurements (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_lab_no TEXT NOT NULL REFERENCES dating_samples(lab_no),
 c14_age REAL NOT NULL,
 c14_sigma REAL NOT NULL,
 method TEXT NOT NULL,
 instrument TEXT NOT NULL DEFAULT '',
 measured_by TEXT NOT NULL DEFAULT '',
 measured_at TEXT NOT NULL,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_measurements_sample ON measurements(sample_lab_no,id);
DROP TRIGGER IF EXISTS trg_measurements_no_update;
CREATE TRIGGER trg_measurements_no_update BEFORE UPDATE ON measurements
BEGIN
 SELECT RAISE(ABORT,'原始测值不可覆盖：measurements 为只追加表');
END;
DROP TRIGGER IF EXISTS trg_measurements_no_delete;
CREATE TRIGGER trg_measurements_no_delete BEFORE DELETE ON measurements
BEGIN
 SELECT RAISE(ABORT,'原始测值不可删除：measurements 为只追加表');
END;
CREATE TABLE IF NOT EXISTS calibration_tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 input_hash TEXT NOT NULL UNIQUE,
 measurement_id INTEGER REFERENCES measurements(id),
 c14_age REAL NOT NULL,
 c14_sigma REAL NOT NULL,
 curve_version TEXT NOT NULL,
 probability REAL NOT NULL,
 algorithm_version TEXT NOT NULL,
 input_summary_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('completed','failed')),
 result_json TEXT NOT NULL DEFAULT '',
 error_code TEXT NOT NULL DEFAULT '',
 error_message TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
DROP TRIGGER IF EXISTS trg_calibration_tasks_no_update;
CREATE TRIGGER trg_calibration_tasks_no_update BEFORE UPDATE ON calibration_tasks
BEGIN
 SELECT RAISE(ABORT,'校准任务结果不可变：calibration_tasks 为只追加表');
END;
CREATE TABLE IF NOT EXISTS determination_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_lab_no TEXT NOT NULL REFERENCES dating_samples(lab_no),
 calibration_task_id INTEGER REFERENCES calibration_tasks(id),
 decision TEXT NOT NULL CHECK(decision IN ('adopted','rejected')),
 reason TEXT NOT NULL DEFAULT '',
 researcher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 superseded_by_id INTEGER REFERENCES determination_decisions(id),
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_sample ON determination_decisions(sample_lab_no,id);
CREATE TABLE IF NOT EXISTS phase_sets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 set_id INTEGER NOT NULL REFERENCES phase_sets(id),
 version_no INTEGER NOT NULL,
 based_on_version_id INTEGER,
 curve_version TEXT NOT NULL,
 input_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published')),
 combined_json TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 published_at TEXT NOT NULL DEFAULT '',
 UNIQUE(set_id,version_no)
);
CREATE TABLE IF NOT EXISTS phase_version_members (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 phase_version_id INTEGER NOT NULL REFERENCES phase_versions(id) ON DELETE CASCADE,
 sample_lab_no TEXT NOT NULL REFERENCES dating_samples(lab_no),
 calibration_task_id INTEGER REFERENCES calibration_tasks(id),
 included INTEGER NOT NULL CHECK(included IN (0,1)),
 exclusion_reason TEXT NOT NULL DEFAULT '',
 note TEXT NOT NULL DEFAULT '',
 UNIQUE(phase_version_id,sample_lab_no)
);
CREATE INDEX IF NOT EXISTS idx_phase_members_version ON phase_version_members(phase_version_id);
DROP TRIGGER IF EXISTS trg_phase_version_published_immutable;
CREATE TRIGGER trg_phase_version_published_immutable BEFORE UPDATE ON phase_versions
WHEN OLD.status='published'
BEGIN
 SELECT RAISE(ABORT,'已发布的阶段证据版本不可变');
END;
DROP TRIGGER IF EXISTS trg_phase_members_published_no_update;
CREATE TRIGGER trg_phase_members_published_no_update BEFORE UPDATE ON phase_version_members
WHEN EXISTS(SELECT 1 FROM phase_versions v WHERE v.id=NEW.phase_version_id AND v.status='published')
BEGIN
 SELECT RAISE(ABORT,'已发布版本的成员证据不可改写');
END;
DROP TRIGGER IF EXISTS trg_phase_members_published_no_delete;
CREATE TRIGGER trg_phase_members_published_no_delete BEFORE DELETE ON phase_version_members
WHEN EXISTS(SELECT 1 FROM phase_versions v WHERE v.id=OLD.phase_version_id AND v.status='published')
BEGIN
 SELECT RAISE(ABORT,'已发布版本的成员证据不可删除');
END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create() -> sqlite3.Connection:
    path = settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def connection() -> sqlite3.Connection:
    value = getattr(_local, "connection", None)
    if value is None:
        value = _create()
        _local.connection = value
    return value


def close_connection() -> None:
    value = getattr(_local, "connection", None)
    if value is not None:
        value.close()
        _local.connection = None


def init_db() -> None:
    connection().executescript(SCHEMA)


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    db = connection()
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
