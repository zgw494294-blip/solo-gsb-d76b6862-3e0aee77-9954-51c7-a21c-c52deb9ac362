"""Request/response schemas and shared time helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

#: Maximum number of items (snapshots + log segments) accepted in one batch.
MAX_BATCH_SIZE = 100


def to_utc(value: datetime) -> datetime:
    """Normalize a datetime to aware UTC. Naive input is assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SnapshotIn(BaseModel):
    """A database snapshot as registered in a batch."""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1, max_length=128)
    created_at: datetime
    applied_seq: StrictInt = Field(ge=0)
    format: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("created_at")
    @classmethod
    def _normalize_created_at(cls, value: datetime) -> datetime:
        return to_utc(value)


class LogSegmentIn(BaseModel):
    """A log segment covering the closed interval [first_seq, last_seq]."""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1, max_length=128)
    created_at: datetime
    format: StrictStr = Field(min_length=1, max_length=64)
    first_seq: StrictInt = Field(ge=1)
    last_seq: StrictInt = Field(ge=1)

    @field_validator("created_at")
    @classmethod
    def _normalize_created_at(cls, value: datetime) -> datetime:
        return to_utc(value)

    @model_validator(mode="after")
    def _check_interval(self) -> "LogSegmentIn":
        if self.last_seq < self.first_seq:
            raise ValueError(
                f"log segment {self.id!r}: last_seq ({self.last_seq}) "
                f"must be >= first_seq ({self.first_seq})"
            )
        return self


class BatchIn(BaseModel):
    """One registration batch. Either everything is stored or nothing is."""

    model_config = ConfigDict(extra="forbid")

    snapshots: List[SnapshotIn] = Field(default_factory=list)
    log_segments: List[LogSegmentIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_batch(self) -> "BatchIn":
        total = len(self.snapshots) + len(self.log_segments)
        if total > MAX_BATCH_SIZE:
            raise ValueError(
                f"batch holds {total} items, at most {MAX_BATCH_SIZE} allowed; batch rejected"
            )
        ids = [s.id for s in self.snapshots] + [s.id for s in self.log_segments]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate id(s) within batch: {duplicates}; batch rejected")
        return self


#: Limits for one protected-moment cleanup request.
MIN_CLEANUP_IDS = 1
MAX_CLEANUP_IDS = 100
MIN_CLEANUP_MOMENTS = 1
MAX_CLEANUP_MOMENTS = 8


class CleanupIn(BaseModel):
    """Batch-delete request guarded by protected UTC moments.

    ``ids`` must hold 1..100 distinct registered record ids (snapshots or
    log segments); ``moments`` must hold 1..8 UTC query times. Duplicate ids
    (or ids absent from the database) reject the whole request.
    """

    model_config = ConfigDict(extra="forbid")

    ids: List[StrictStr] = Field(min_length=MIN_CLEANUP_IDS, max_length=MAX_CLEANUP_IDS)
    moments: List[datetime] = Field(
        min_length=MIN_CLEANUP_MOMENTS, max_length=MAX_CLEANUP_MOMENTS
    )

    @field_validator("ids")
    @classmethod
    def _check_ids(cls, value: List[str]) -> List[str]:
        empty = [i for i in value if not i]
        if empty:
            raise ValueError("ids must be non-empty strings")
        duplicates = sorted({i for i in value if value.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate id(s) in request: {duplicates}; request rejected")
        return value

    @field_validator("moments")
    @classmethod
    def _normalize_moments(cls, value: List[datetime]) -> List[datetime]:
        return [to_utc(v) for v in value]
