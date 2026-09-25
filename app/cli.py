from __future__ import annotations

import argparse
import json
import os

from fastapi.testclient import TestClient

from app.database import connection, init_db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "check-db", "smoke"])
    args = parser.parse_args()
    if args.command == "init-db":
        init_db()
        print(json.dumps({"status": "initialized"}, ensure_ascii=False))
        return 0
    if args.command == "check-db":
        init_db()
        db = connection()
        print(json.dumps({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]}, ensure_ascii=False))
        return 0
    from app.main import app
    import tempfile
    from app.database import close_connection
    tmp_dir = tempfile.mkdtemp(prefix="dating-smoke-")
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = os.path.join(tmp_dir, "smoke.db")
    close_connection()
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        # 年代证据主链路冒烟：用户→项目→登记→交接→测值→校准
        client.post("/api/users", json={"username": "smoke", "display_name": "冒烟", "password": "SmokePass!234"})
        token = client.post("/api/sessions", json={"username": "smoke", "password": "SmokePass!234"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        project_id = client.post("/api/projects", json={"code": "SMOKE", "name": "冒烟项目", "site_name": "遗址"}, headers=headers).json()["id"]
        client.post("/api/dating/samples", json={
            "lab_no": "SMK-0001", "project_id": project_id, "context_type": "stratum",
            "context_name": "T1第4层", "material": "炭化种子", "collected_at": "2026-09-01", "custodian": "发掘队",
        }, headers=headers)
        client.post("/api/dating/samples/SMK-0001/seal-events", json={
            "event_type": "transfer", "from_party": "发掘队", "to_party": "年代学实验室",
        }, headers=headers)
        measurement_id = client.post("/api/dating/samples/SMK-0001/measurements", json={
            "c14_age": 2500, "c14_sigma": 30, "method": "AMS", "measured_at": "2026-09-10",
        }, headers=headers).json()["id"]
        task = client.post(f"/api/dating/samples/SMK-0001/measurements/{measurement_id}/calibrate",
                           json={}, headers=headers).json()
        dating = {
            "lab_no": "SMK-0001",
            "curve": task["curve_version"],
            "algorithm": task["algorithm_version"],
            "hpd_intervals": len(task["result"]["hpd_intervals"]),
            "modes": len(task["result"]["modes"]),
            "idempotent_task_id": client.post("/api/dating/calibrations", json={"c14_age": 2500, "c14_sigma": 30}).json()["id"],
        }
        print(json.dumps({
            "root": root.json(),
            "health": health.json(),
            "status_codes": [root.status_code, health.status_code],
            "dating": dating,
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
