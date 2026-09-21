"""Pydantic models: request/response schemas and validation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from app.services.tides import to_epoch


def _utc_z(value: datetime) -> str:
    """Serialise a datetime as ISO-8601 with a trailing ``Z`` (UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


UtcZ = Annotated[
    datetime,
    PlainSerializer(_utc_z, return_type=str),
]


class TideSample(BaseModel):
    time: datetime = Field(..., description="潮位采样时间（无时区按 UTC 处理）")
    height_m: float = Field(..., description="潮高（米，需与吃水/富余同一垂直基准面）")

    model_config = ConfigDict(json_schema_extra={
        "example": {"time": "2026-09-21T00:00:00Z", "height_m": 2.15}
    })


class WindowRequest(BaseModel):
    samples: list[TideSample] = Field(..., min_length=2, description="按 UTC 递增的潮位样本")
    query_start: datetime = Field(..., description="查询起始时间（UTC）")
    query_end: datetime = Field(..., description="查询结束时间（UTC）")
    draft_m: float = Field(..., ge=0, description="船舶吃水（米）")
    clearance_m: float = Field(..., ge=0, description="船底最小富余水深（米）")
    max_gap_seconds: float = Field(
        ..., gt=0, description="相邻样本最大允许间隔（秒），超过则禁止插值"
    )

    @model_validator(mode="after")
    def _validate(self) -> "WindowRequest":
        # Naive datetimes are interpreted as UTC before any comparison.
        epochs = [to_epoch(s.time) for s in self.samples]
        qs = to_epoch(self.query_start)
        qe = to_epoch(self.query_end)

        for i in range(1, len(epochs)):
            if epochs[i] <= epochs[i - 1]:
                raise ValueError("samples.time 必须严格按 UTC 递增")
        if qe <= qs:
            raise ValueError("query_end 必须晚于 query_start")
        for name, value in (
            ("draft_m", self.draft_m),
            ("clearance_m", self.clearance_m),
            ("max_gap_seconds", self.max_gap_seconds),
        ):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{name} 必须为有限数值")
        for i, s in enumerate(self.samples):
            if s.height_m != s.height_m:
                raise ValueError(f"samples[{i}].height_m 不能为 NaN")
        return self


class WindowOut(BaseModel):
    start: UtcZ
    end: UtcZ
    start_height_m: float
    end_height_m: float
    min_height_m: float
    duration_seconds: int


class UnknownOut(BaseModel):
    start: UtcZ
    end: UtcZ
    reason: str
    duration_seconds: float


class ResultOut(BaseModel):
    threshold_m: float
    max_gap_seconds: float
    windows: list[WindowOut]
    unknown: list[UnknownOut]


class TaskOut(BaseModel):
    task_id: str
    status: str
    created_at: UtcZ
    request: WindowRequest
    result: ResultOut | None = None
    error: str | None = None
