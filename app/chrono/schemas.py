from __future__ import annotations

from pydantic import BaseModel, Field

CONTEXT_TYPES = {"stratum", "pit", "tomb", "ditch", "surface", "other"}
SEAL_EVENT_TYPES = {"seal", "handover", "receive", "open", "damage", "reseal"}


class SampleCreate(BaseModel):
    project_id: int | None = None
    field_code: str = Field(..., min_length=1, max_length=80)
    context_type: str = Field(..., pattern=f"^({'|'.join(sorted(CONTEXT_TYPES))})$")
    context_name: str = Field(..., min_length=1, max_length=120)
    material: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class SealEventCreate(BaseModel):
    event_type: str = Field(..., pattern=f"^({'|'.join(sorted(SEAL_EVENT_TYPES))})$")
    seal_id: str = Field(default="", max_length=80)
    from_party: str = Field(default="", max_length=120)
    to_party: str = Field(default="", max_length=120)
    actor: str = Field(default="", max_length=120)
    seal_intact: bool = True
    note: str = Field(default="", max_length=500)


class PretreatmentCreate(BaseModel):
    method: str = Field(..., min_length=1, max_length=120)
    operator: str = Field(..., min_length=1, max_length=80)
    conclusion: str = Field(..., pattern="^(pass|caution|fail)$")
    risks: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=500)


class MeasurementCreate(BaseModel):
    measurement_no: str = Field(default="", max_length=60)
    c14_age_bp: float = Field(..., ge=0, le=100000)
    c14_error_bp: float = Field(..., gt=0, le=10000)
    delta_c13: float | None = Field(default=None, ge=-50, le=10)
    instrument: str = Field(default="", max_length=120)
    operator: str = Field(default="", max_length=80)


class CalibrationRequest(BaseModel):
    measurement_no: str = Field(..., min_length=1, max_length=60)
    curve_name: str = Field(..., min_length=1, max_length=60)
    reservoir_offset_bp: float = Field(default=0.0, ge=-1000, le=10000)
    reservoir_error_bp: float = Field(default=0.0, ge=0, le=2000)


class PublicationRequest(BaseModel):
    task_no: str = Field(..., min_length=1, max_length=60)
    decision: str = Field(..., pattern="^(adopted|rejected)$")
    reason: str = Field(default="", max_length=800)
    boundary_acknowledged: bool = False


class PhaseSetCreate(BaseModel):
    phase_code: str = Field(..., min_length=2, max_length=40)
    name: str = Field(..., min_length=1, max_length=120)
    project_id: int | None = None


class PhaseMemberSpec(BaseModel):
    sample_id: int
    excluded: bool = False
    exclude_reason: str = Field(default="", max_length=500)


class PhaseVersionCreate(BaseModel):
    curve_name: str = Field(..., min_length=1, max_length=60)
    members: list[PhaseMemberSpec] = Field(..., min_length=1)
    note: str = Field(default="", max_length=500)
