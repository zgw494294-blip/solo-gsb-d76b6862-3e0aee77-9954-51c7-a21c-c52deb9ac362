"""FastAPI application: batch registration and recovery planning."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query

from . import db
from .models import BatchIn, CleanupIn, to_utc
from .planner import build_plan, best_endpoint


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # initialize the SQLite schema on startup
    yield


app = FastAPI(
    title="Recovery Planner",
    version="1.0.0",
    description=(
        "Register database snapshots and log segments in batches, ask for "
        "the optimal recovery plan (snapshot + continuous same-format log "
        "replay) at a given UTC time, and batch-delete records while "
        "verifying that protected recovery moments stay intact."
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


@app.post("/cleanup")
def cleanup(cleanup_in: CleanupIn) -> dict:
    """Batch-delete registered snapshots/log segments guarded by protected moments.

    For each protected UTC moment the best recovery endpoint before the
    deletion is compared with the best endpoint after deleting all requested
    ids. The deletion happens only when no moment's endpoint decreases
    (``None`` marks "no recovery point" and lies below every sequence
    number). If any moment decreases, nothing is deleted and every decreased
    moment with its before/after endpoints is returned. The comparison and
    the deletion run in one transaction against one database state, so a
    failure never leaves a partial deletion.
    """
    ids = cleanup_in.ids
    moments = cleanup_in.moments
    conn = db.connect()
    try:
        # Acquire the write lock up front and take a single consistent
        # snapshot: all planning below and the final delete see this state.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = db.all_ids(conn)
            unknown = sorted(set(ids) - existing)
            if unknown:
                raise HTTPException(
                    status_code=409,
                    detail=f"id(s) not registered: {unknown}; request rejected",
                )

            snapshots, segments = db.load_all(conn)
            id_set = set(ids)
            remaining_snapshots = [s for s in snapshots if s.id not in id_set]
            remaining_segments = [s for s in segments if s.id not in id_set]

            decreased = []
            retained = []
            for at in moments:
                before = best_endpoint(snapshots, segments, at)
                after = best_endpoint(remaining_snapshots, remaining_segments, at)
                if before is not None and (after is None or after < before):
                    decreased.append(
                        {"at": at.isoformat(), "before": before, "after": after}
                    )
                retained.append({"at": at.isoformat(), "endpoint": after})

            if decreased:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "best recovery endpoint decreases at one or more "
                            "protected moments; nothing was deleted"
                        ),
                        "decreased": decreased,
                    },
                )

            db.delete_records(conn, ids)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"deleted_ids": ids, "endpoints": retained}
    finally:
        conn.close()
