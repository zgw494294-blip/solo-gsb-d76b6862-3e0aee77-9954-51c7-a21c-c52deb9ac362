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
