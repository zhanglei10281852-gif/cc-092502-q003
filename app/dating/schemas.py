"""年代证据服务的 HTTP 请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field

CONTEXT_TYPES = {"stratum", "pit"}
SEVERITIES = {"low", "medium", "high"}


class SampleRegister(BaseModel):
    lab_no: str = Field(..., min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    project_id: int
    context_type: str = Field(..., pattern="^(stratum|pit)$")
    context_name: str = Field(..., min_length=1, max_length=120)
    material: str = Field(..., min_length=1, max_length=120)
    sample_note: str = Field(default="", max_length=500)
    collected_at: str = Field(..., min_length=4, max_length=40)
    custodian: str = Field(..., min_length=1, max_length=120)


class SealEventCreate(BaseModel):
    event_type: str = Field(..., pattern="^(seal|transfer|break|reseal)$")
    from_party: str = Field(default="", max_length=120)
    to_party: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=500)


class RiskCreate(BaseModel):
    risk_code: str = Field(..., min_length=2, max_length=60)
    severity: str = Field(..., pattern="^(low|medium|high)$")
    note: str = Field(default="", max_length=500)


class RiskResolve(BaseModel):
    resolution_note: str = Field(default="", max_length=500)


class MeasurementCreate(BaseModel):
    c14_age: float = Field(..., ge=-10000, le=100000)
    c14_sigma: float = Field(..., gt=0, le=10000)
    method: str = Field(..., min_length=1, max_length=80)
    instrument: str = Field(default="", max_length=120)
    measured_by: str = Field(default="", max_length=120)
    measured_at: str = Field(..., min_length=4, max_length=40)


class CalibrationRequest(BaseModel):
    c14_age: float = Field(..., ge=-10000, le=100000)
    c14_sigma: float = Field(..., gt=0, le=10000)
    curve_version: str | None = None
    probability: float = Field(default=0.954, gt=0, lt=1)


class MeasurementCalibrationRequest(BaseModel):
    curve_version: str | None = None
    probability: float = Field(default=0.954, gt=0, lt=1)


class DecisionCreate(BaseModel):
    calibration_task_id: int | None = None
    decision: str = Field(..., pattern="^(adopted|rejected)$")
    reason: str = Field(default="", max_length=500)


class PhaseSetCreate(BaseModel):
    project_id: int
    name: str = Field(..., min_length=1, max_length=120)


class PhaseMemberSpec(BaseModel):
    sample_lab_no: str = Field(..., min_length=3, max_length=40)
    included: bool = True
    exclusion_reason: str = Field(default="", max_length=500)
    calibration_task_id: int | None = None
    note: str = Field(default="", max_length=500)


class PhaseVersionCreate(BaseModel):
    curve_version: str | None = None
    members: list[PhaseMemberSpec] = Field(default_factory=list)
    note: str = Field(default="", max_length=500)


class PhasePublish(BaseModel):
    note: str = Field(default="", max_length=500)
