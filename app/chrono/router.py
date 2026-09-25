from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.chrono.curves import list_curves
from app.chrono.schemas import (
    CalibrationRequest,
    MeasurementCreate,
    PhaseSetCreate,
    PhaseVersionCreate,
    PretreatmentCreate,
    PublicationRequest,
    SampleCreate,
    SealEventCreate,
)
from app.chrono.service import ChronoService
from app.main import current_user

router = APIRouter(prefix="/api/chrono", tags=["chrono"])


@router.get("/curves")
def curves():
    return {"data": list_curves()}


@router.post("/samples", status_code=201)
def register_sample(payload: SampleCreate, user=Depends(current_user)):
    return ChronoService().register_sample(payload.model_dump(), user["id"])


@router.get("/samples")
def list_samples(project_id: int | None = Query(default=None), context_type: str | None = Query(default=None), user=Depends(current_user)):
    del user
    return ChronoService().list_samples(project_id, context_type)


@router.get("/samples/{lab_no}")
def get_sample(lab_no: str, user=Depends(current_user)):
    del user
    return ChronoService().get_sample(lab_no)


@router.post("/samples/{lab_no}/seal-events", status_code=201)
def add_seal_event(lab_no: str, payload: SealEventCreate, user=Depends(current_user)):
    return ChronoService().add_seal_event(lab_no, payload.model_dump(), user["id"])


@router.post("/samples/{lab_no}/pretreatments", status_code=201)
def add_pretreatment(lab_no: str, payload: PretreatmentCreate, user=Depends(current_user)):
    return ChronoService().add_pretreatment(lab_no, payload.model_dump(), user["id"])


@router.post("/samples/{lab_no}/measurements", status_code=201)
def add_measurement(lab_no: str, payload: MeasurementCreate, user=Depends(current_user)):
    return ChronoService().add_measurement(lab_no, payload.model_dump(), user["id"])


@router.post("/samples/{lab_no}/calibrations", status_code=201)
def submit_calibration(lab_no: str, payload: CalibrationRequest, user=Depends(current_user)):
    return ChronoService().submit_calibration(lab_no, payload.model_dump(), user["id"])


@router.get("/calibrations/{task_no}")
def get_calibration(task_no: str, user=Depends(current_user)):
    del user
    return ChronoService().get_calibration(task_no)


@router.post("/samples/{lab_no}/publication", status_code=201)
def publish_decision(lab_no: str, payload: PublicationRequest, user=Depends(current_user)):
    return ChronoService().publish_decision(lab_no, payload.model_dump(), user["id"])


@router.post("/phases", status_code=201)
def create_phase_set(payload: PhaseSetCreate, user=Depends(current_user)):
    return ChronoService().create_phase_set(payload.model_dump(), user["id"])


@router.get("/phases/{phase_code}")
def get_phase_set(phase_code: str, user=Depends(current_user)):
    del user
    return ChronoService().get_phase_set(phase_code)


@router.post("/phases/{phase_code}/versions", status_code=201)
def create_phase_version(phase_code: str, payload: PhaseVersionCreate, user=Depends(current_user)):
    return ChronoService().create_phase_version(phase_code, payload.model_dump(), user["id"])


@router.post("/phases/{phase_code}/versions/{version_no}/publish")
def publish_phase_version(phase_code: str, version_no: int, user=Depends(current_user)):
    return ChronoService().publish_phase_version(phase_code, version_no, user["id"])


@router.get("/phases/{phase_code}/versions/{version_no}")
def get_phase_version(phase_code: str, version_no: int, user=Depends(current_user)):
    del user
    return ChronoService().get_version(phase_code, version_no)


@router.get("/phases/{phase_code}/diff")
def diff_phase_versions(phase_code: str, from_version: int = Query(..., alias="from"), to_version: int = Query(..., alias="to"), user=Depends(current_user)):
    del user
    return ChronoService().diff_versions(phase_code, from_version, to_version)
