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


class MaintenanceBlock(BaseModel):
    """UTC time span closed for maintenance (no traffic)."""

    start: datetime = Field(..., description="封航起始时间（UTC，半开区间起点）")
    end: datetime = Field(..., description="封航结束时间（UTC，半开区间终点）")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "start": "2026-09-21T02:30:00Z",
            "end": "2026-09-21T03:00:00Z",
        }
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
    transit_duration_seconds: float | None = Field(
        default=None,
        ge=0,
        description="全程航程占用时长（秒）。传入后结果含 departure_windows："
        "出发时刻 d 的占用区间按 [d, d+transit_duration_seconds) 处理",
    )
    maintenance_blocks: list[MaintenanceBlock] | None = Field(
        default=None,
        description="维护封航区间（UTC 起止）；自动裁剪到查询范围并合并重叠/首尾相接区间。"
        "仅在同时给出 transit_duration_seconds 时参与 departure_windows 计算",
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
        if self.transit_duration_seconds is not None and (
            self.transit_duration_seconds != self.transit_duration_seconds
            or self.transit_duration_seconds == float("inf")
        ):
            raise ValueError("transit_duration_seconds 必须为有限的非负数值")
        for i, s in enumerate(self.samples):
            if s.height_m != s.height_m:
                raise ValueError(f"samples[{i}].height_m 不能为 NaN")
        if self.maintenance_blocks:
            for i, block in enumerate(self.maintenance_blocks):
                if to_epoch(block.end) <= to_epoch(block.start):
                    raise ValueError(
                        f"maintenance_blocks[{i}].end 必须晚于 start"
                    )
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "samples": [
                    {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": hm}
                    for h, hm in (
                        (0, 1.0), (1, 3.0), (2, 6.0), (3, 8.0),
                        (4, 8.0), (5, 6.0), (6, 3.0), (7, 1.0),
                    )
                ],
                "query_start": "2026-09-21T00:00:00Z",
                "query_end": "2026-09-21T07:00:00Z",
                "draft_m": 4.0,
                "clearance_m": 1.0,
                "max_gap_seconds": 3700,
                "transit_duration_seconds": 3600,
                "maintenance_blocks": [
                    {"start": "2026-09-21T04:30:00Z",
                     "end": "2026-09-21T05:00:00Z"},
                    {"start": "2026-09-21T05:00:00Z",
                     "end": "2026-09-21T05:30:00Z"},
                    {"start": "2026-09-21T06:30:00Z",
                     "end": "2026-09-21T09:00:00Z"},
                ],
            }
        }
    )


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


class DepartureWindowOut(BaseModel):
    """可行出发时段（半开区间 [start, end)，整秒）。"""

    start: UtcZ = Field(..., description="最早可行出发时刻（UTC，整秒）")
    end: UtcZ = Field(
        ...,
        description="出发时段右端点（UTC，半开区间：该时刻本身不可出发）",
    )
    duration_seconds: int = Field(
        ..., description="可行出发秒数（end - start）"
    )


class ResultOut(BaseModel):
    threshold_m: float
    max_gap_seconds: float
    windows: list[WindowOut]
    unknown: list[UnknownOut]
    departure_windows: list[DepartureWindowOut] = Field(
        default_factory=list,
        description="可行出发时段：占用 [d, d+transit_duration_seconds) "
        "全程落在同一安全窗口、且不与任何维护封航区间相交；"
        "未传 transit_duration_seconds 时为空列表",
    )


class TaskOut(BaseModel):
    task_id: str
    status: str
    created_at: UtcZ
    request: WindowRequest
    result: ResultOut | None = None
    error: str | None = None
