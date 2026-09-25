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

-- ===========================================================================
-- 年代证据模块：取样登记、封签交接、前处理、测值、校准、采用决定、阶段证据集
-- ===========================================================================
CREATE TABLE IF NOT EXISTS counters (
 name TEXT PRIMARY KEY,
 value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dating_samples (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 lab_no TEXT NOT NULL UNIQUE,
 project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
 field_code TEXT NOT NULL,
 context_type TEXT NOT NULL CHECK(context_type IN ('stratum','pit','tomb','ditch','surface','other')),
 context_name TEXT NOT NULL,
 material TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 chain_status TEXT NOT NULL DEFAULT 'registered'
   CHECK(chain_status IN ('registered','sealed','in_transit','received','opened','quarantined')),
 registered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seal_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_id INTEGER NOT NULL REFERENCES dating_samples(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN ('seal','handover','receive','open','damage','reseal')),
 seal_id TEXT NOT NULL DEFAULT '',
 from_party TEXT NOT NULL DEFAULT '',
 to_party TEXT NOT NULL DEFAULT '',
 actor TEXT NOT NULL DEFAULT '',
 seal_intact INTEGER NOT NULL DEFAULT 1,
 note TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(sample_id,seq)
);
CREATE INDEX IF NOT EXISTS idx_seal_sample ON seal_events(sample_id,seq);
CREATE TABLE IF NOT EXISTS pretreatment_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_id INTEGER NOT NULL REFERENCES dating_samples(id) ON DELETE CASCADE,
 method TEXT NOT NULL,
 operator TEXT NOT NULL,
 conclusion TEXT NOT NULL CHECK(conclusion IN ('pass','caution','fail')),
 risks_json TEXT NOT NULL DEFAULT '[]',
 note TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pretreat_sample ON pretreatment_records(sample_id,id);
CREATE TABLE IF NOT EXISTS measurements (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 measurement_no TEXT NOT NULL UNIQUE,
 sample_id INTEGER NOT NULL REFERENCES dating_samples(id) ON DELETE CASCADE,
 c14_age_bp REAL NOT NULL,
 c14_error_bp REAL NOT NULL,
 delta_c13 REAL,
 instrument TEXT NOT NULL DEFAULT '',
 operator TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meas_sample ON measurements(sample_id,id);
CREATE TRIGGER IF NOT EXISTS trg_meas_no_update BEFORE UPDATE ON measurements
BEGIN SELECT RAISE(ABORT,'原始测值记录不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_meas_no_delete BEFORE DELETE ON measurements
BEGIN SELECT RAISE(ABORT,'原始测值记录不可删除'); END;
CREATE TABLE IF NOT EXISTS calibration_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_no TEXT NOT NULL UNIQUE,
 measurement_id INTEGER NOT NULL REFERENCES measurements(id) ON DELETE CASCADE,
 curve_name TEXT NOT NULL,
 curve_version TEXT NOT NULL,
 curve_checksum TEXT NOT NULL,
 reservoir_offset_bp REAL NOT NULL DEFAULT 0,
 reservoir_error_bp REAL NOT NULL DEFAULT 0,
 input_hash TEXT NOT NULL UNIQUE,
 input_summary_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('done','failed')),
 result_json TEXT NOT NULL DEFAULT '{}',
 error_text TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 1,
 algorithm_name TEXT NOT NULL,
 algorithm_version TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_id INTEGER NOT NULL UNIQUE REFERENCES dating_samples(id) ON DELETE CASCADE,
 calibration_job_id INTEGER NOT NULL REFERENCES calibration_jobs(id) ON DELETE CASCADE,
 decision TEXT NOT NULL CHECK(decision IN ('adopted','rejected')),
 reason TEXT NOT NULL DEFAULT '',
 boundary_acknowledged INTEGER NOT NULL DEFAULT 0,
 published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 published_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_pub_no_update BEFORE UPDATE ON publications
BEGIN SELECT RAISE(ABORT,'已发布的采用决定不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_pub_no_delete BEFORE DELETE ON publications
BEGIN SELECT RAISE(ABORT,'已发布的采用决定不可删除'); END;
CREATE TABLE IF NOT EXISTS phase_sets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 phase_code TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL,
 project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 phase_set_id INTEGER NOT NULL REFERENCES phase_sets(id) ON DELETE CASCADE,
 version_no INTEGER NOT NULL,
 curve_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published')),
 input_hash TEXT NOT NULL,
 published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 published_at TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(phase_set_id,version_no)
);
CREATE TRIGGER IF NOT EXISTS trg_phase_version_no_update AFTER UPDATE ON phase_versions
WHEN OLD.status='published'
BEGIN SELECT RAISE(ABORT,'已发布的阶段证据版本不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_phase_version_no_delete BEFORE DELETE ON phase_versions
WHEN OLD.status='published'
BEGIN SELECT RAISE(ABORT,'已发布的阶段证据版本不可删除'); END;
CREATE TABLE IF NOT EXISTS phase_members (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 phase_version_id INTEGER NOT NULL REFERENCES phase_versions(id) ON DELETE CASCADE,
 position INTEGER NOT NULL,
 sample_id INTEGER NOT NULL REFERENCES dating_samples(id) ON DELETE CASCADE,
 calibration_job_id INTEGER NOT NULL REFERENCES calibration_jobs(id) ON DELETE CASCADE,
 excluded INTEGER NOT NULL DEFAULT 0,
 exclude_reason TEXT NOT NULL DEFAULT '',
 UNIQUE(phase_version_id,sample_id)
);
CREATE INDEX IF NOT EXISTS idx_phase_members_version ON phase_members(phase_version_id,position);
CREATE TRIGGER IF NOT EXISTS trg_phase_member_no_update BEFORE UPDATE ON phase_members
WHEN EXISTS (SELECT 1 FROM phase_versions pv WHERE pv.id=OLD.phase_version_id AND pv.status='published')
BEGIN SELECT RAISE(ABORT,'已发布版本的成员不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_phase_member_no_delete BEFORE DELETE ON phase_members
WHEN EXISTS (SELECT 1 FROM phase_versions pv WHERE pv.id=OLD.phase_version_id AND pv.status='published')
BEGIN SELECT RAISE(ABORT,'已发布版本的成员不可删除'); END;
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
