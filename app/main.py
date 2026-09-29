"""FastAPI application: batch registration and recovery planning."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query

from . import db
from .models import BatchIn, to_utc
from .planner import build_plan


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # initialize the SQLite schema on startup
    yield


app = FastAPI(
    title="Recovery Planner",
    version="1.0.0",
    description=(
        "Register database snapshots and log segments in batches, then ask for "
        "the optimal recovery plan (snapshot + continuous same-format log "
        "replay) at a given UTC time."
    ),
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/batches", status_code=201)
def register_batch(batch: BatchIn) -> dict:
    """Register a batch of snapshots and log segments.

    The batch is stored atomically: duplicate ids (within the batch or
    against already-registered items), invalid fields, or more than 100
    items cause the whole batch to be rejected.
    """
    conn = db.connect()
    try:
        incoming = [s.id for s in batch.snapshots] + [s.id for s in batch.log_segments]
        conflicts = sorted(set(incoming) & db.all_ids(conn))
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=f"id(s) already registered: {conflicts}; batch rejected",
            )
        db.insert_batch(conn, batch)
        return {
            "stored_snapshots": len(batch.snapshots),
            "stored_log_segments": len(batch.log_segments),
        }
    finally:
        conn.close()


@app.get("/recovery-plan")
def recovery_plan(
    at: datetime = Query(
        ...,
        description="UTC query time (ISO 8601). Only items created at or before this moment are used.",
    ),
) -> dict:
    """Compute the optimal recovery plan for the given UTC query time."""
    at_utc = to_utc(at)
    conn = db.connect()
    try:
        snapshots, segments = db.load_all(conn)
    finally:
        conn.close()

    plan = build_plan(snapshots, segments, at_utc)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "no recovery point: no snapshot created at or before "
                f"{at_utc.isoformat()}"
            ),
        )

    snap = plan.snapshot
    return {
        "snapshot": {
            "id": snap.id,
            "created_at": snap.created_at.isoformat(),
            "applied_seq": snap.applied_seq,
            "format": snap.format,
        },
        "replay": [
            {"id": step.segment_id, "from_seq": step.from_seq, "to_seq": step.to_seq}
            for step in plan.steps
        ],
        "endpoint": plan.endpoint,
    }
