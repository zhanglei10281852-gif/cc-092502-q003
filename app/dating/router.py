"""年代证据服务 HTTP 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.dating import service
from app.dating.schemas import (
    CalibrationRequest,
    DecisionCreate,
    MeasurementCalibrationRequest,
    MeasurementCreate,
    PhasePublish,
    PhaseSetCreate,
    PhaseVersionCreate,
    RiskCreate,
    RiskResolve,
    SealEventCreate,
    SampleRegister,
)
from app.service import ResearchService

router = APIRouter(prefix="/api/dating", tags=["dating"])


def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])


@router.get("/curves")
def curves():
    return service.list_curves()


@router.get("/curves/{version}")
def curve_detail(version: str):
    return service.get_curve(version)


@router.post("/samples", status_code=201)
def register_sample(payload: SampleRegister, user=Depends(current_user)):
    return service.register_sample(payload.model_dump(), user["id"])


@router.get("/samples")
def samples(project_id: int | None = Query(default=None), user=Depends(current_user)):
    del user
    return service.list_samples(project_id)


@router.get("/samples/{lab_no}")
def sample_detail(lab_no: str, user=Depends(current_user)):
    del user
    return service.get_sample(lab_no.strip().upper())


@router.post("/samples/{lab_no}/seal-events", status_code=201)
def seal_event(lab_no: str, payload: SealEventCreate, user=Depends(current_user)):
    return service.add_seal_event(lab_no.strip().upper(), payload.model_dump(), user["id"])


@router.post("/samples/{lab_no}/risks", status_code=201)
def add_risk(lab_no: str, payload: RiskCreate, user=Depends(current_user)):
    return service.add_risk(lab_no.strip().upper(), payload.model_dump(), user["id"])


@router.post("/samples/{lab_no}/risks/{risk_id}/resolve")
def resolve_risk(lab_no: str, risk_id: int, payload: RiskResolve, user=Depends(current_user)):
    return service.resolve_risk(lab_no.strip().upper(), risk_id, payload.model_dump(), user["id"])


@router.post("/samples/{lab_no}/measurements", status_code=201)
def add_measurement(lab_no: str, payload: MeasurementCreate, user=Depends(current_user)):
    return service.add_measurement(lab_no.strip().upper(), payload.model_dump(), user["id"])


@router.post("/calibrations", status_code=201)
def calibrate_direct(payload: CalibrationRequest):
    return service.calibrate_direct(payload.model_dump())


@router.post("/samples/{lab_no}/measurements/{measurement_id}/calibrate", status_code=201)
def calibrate_measurement(lab_no: str, measurement_id: int, payload: MeasurementCalibrationRequest, user=Depends(current_user)):
    return service.calibrate_measurement(lab_no.strip().upper(), measurement_id, payload.model_dump(), user["id"])


@router.get("/calibration-tasks/{task_id}")
def calibration_task(task_id: int, user=Depends(current_user)):
    del user
    return service.get_task(task_id)


@router.post("/samples/{lab_no}/decisions", status_code=201)
def create_decision(lab_no: str, payload: DecisionCreate, user=Depends(current_user)):
    return service.create_decision(lab_no.strip().upper(), payload.model_dump(), user["id"])


@router.get("/samples/{lab_no}/decisions")
def list_decisions(lab_no: str, user=Depends(current_user)):
    del user
    return service.list_decisions(lab_no.strip().upper())


@router.post("/phase-sets", status_code=201)
def create_phase_set(payload: PhaseSetCreate, user=Depends(current_user)):
    return service.create_phase_set(payload.model_dump(), user["id"])


@router.get("/phase-sets")
def list_phase_sets(project_id: int | None = Query(default=None), user=Depends(current_user)):
    del user
    return service.list_phase_sets(project_id)


@router.get("/phase-sets/{set_id}")
def phase_set(set_id: int, user=Depends(current_user)):
    del user
    return service.get_phase_set(set_id)


@router.post("/phase-sets/{set_id}/versions", status_code=201)
def create_phase_version(set_id: int, payload: PhaseVersionCreate, user=Depends(current_user)):
    return service.create_phase_version(set_id, payload.model_dump(), user["id"])


@router.get("/phase-versions/{version_id}")
def phase_version(version_id: int, user=Depends(current_user)):
    del user
    return service.get_phase_version(version_id)


@router.post("/phase-versions/{version_id}/publish")
def publish_phase_version(version_id: int, payload: PhasePublish, user=Depends(current_user)):
    return service.publish_phase_version(version_id, payload.model_dump(), user["id"])


@router.get("/phase-versions/{version_id}/compare/{other_id}")
def compare_phase_versions(version_id: int, other_id: int, user=Depends(current_user)):
    del user
    return service.compare_phase_versions(version_id, other_id)
