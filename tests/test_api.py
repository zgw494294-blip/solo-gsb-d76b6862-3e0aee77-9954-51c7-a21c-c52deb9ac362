"""End-to-end tests for the recovery planning API."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DB_PATH", ":memory:")  # placeholder, overridden per test

from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def snap(id, created_at, applied_seq, fmt="wal-v1"):
    return {
        "id": id,
        "created_at": created_at,
        "applied_seq": applied_seq,
        "format": fmt,
    }


def seg(id, created_at, first, last, fmt="wal-v1"):
    return {
        "id": id,
        "created_at": created_at,
        "format": fmt,
        "first_seq": first,
        "last_seq": last,
    }


def register(client, snapshots=(), segments=()):
    return client.post(
        "/batches", json={"snapshots": list(snapshots), "log_segments": list(segments)}
    )


def plan(client, at):
    return client.get("/recovery-plan", params={"at": at})


# ---------------------------------------------------------------- batches


def test_register_batch_ok(client):
    r = register(
        client,
        snapshots=[snap("s1", "2026-01-01T00:00:00Z", 5)],
        segments=[seg("l1", "2026-01-01T00:00:00Z", 6, 9)],
    )
    assert r.status_code == 201
    assert r.json() == {"stored_snapshots": 1, "stored_log_segments": 1}


def test_duplicate_ids_within_batch_rejected(client):
    r = register(
        client,
        snapshots=[snap("x", "2026-01-01T00:00:00Z", 1)],
        segments=[seg("x", "2026-01-01T00:00:00Z", 2, 3)],
    )
    assert r.status_code == 422
    # nothing was stored: the same ids can be registered afterwards
    assert register(client, snapshots=[snap("x", "2026-01-01T00:00:00Z", 1)]).status_code == 201


def test_duplicate_id_against_database_rejected(client):
    assert register(client, snapshots=[snap("s1", "2026-01-01T00:00:00Z", 1)]).status_code == 201
    r = register(
        client,
        snapshots=[snap("s2", "2026-01-01T00:00:00Z", 2)],
        segments=[seg("s1", "2026-01-01T00:00:00Z", 2, 3)],
    )
    assert r.status_code == 409
    # whole batch rolled back: s2 was not stored either
    assert register(client, snapshots=[snap("s2", "2026-01-01T00:00:00Z", 2)]).status_code == 201


def test_batch_larger_than_100_rejected(client):
    segments = [seg(f"l{i}", "2026-01-01T00:00:00Z", i, i) for i in range(1, 102)]
    r = register(client, segments=segments)
    assert r.status_code == 422
    # exactly 100 items is fine
    assert register(client, segments=segments[:100]).status_code == 201


@pytest.mark.parametrize(
    "snapshot",
    [
        snap("s", "2026-01-01T00:00:00Z", -1),          # negative applied_seq
        snap("s", "2026-01-01T00:00:00Z", True),        # bool is not an int
        snap("s", "2026-01-01T00:00:00Z", 1.5),         # float is not an int
        snap("s", "2026-01-01T00:00:00Z", "5"),         # string is not an int
        snap("s", "not-a-time", 1),                     # bad timestamp
        snap("", "2026-01-01T00:00:00Z", 1),            # empty id
        snap("s", "2026-01-01T00:00:00Z", 1, fmt=""),   # empty format
    ],
)
def test_invalid_snapshot_fields_rejected(client, snapshot):
    assert register(client, snapshots=[snapshot]).status_code == 422


@pytest.mark.parametrize(
    "segment",
    [
        seg("l", "2026-01-01T00:00:00Z", 0, 3),    # first_seq must be positive
        seg("l", "2026-01-01T00:00:00Z", 5, 3),    # last_seq < first_seq
        seg("l", "2026-01-01T00:00:00Z", 1, True),
        seg("l", "2026-01-01T00:00:00Z", 1, "9"),
    ],
)
def test_invalid_segment_fields_rejected(client, segment):
    assert register(client, segments=[segment]).status_code == 422


def test_unknown_field_rejected(client):
    bad = snap("s", "2026-01-01T00:00:00Z", 1)
    bad["unexpected"] = 1
    assert register(client, snapshots=[bad]).status_code == 422


# ------------------------------------------------------------ recovery plan


def test_plan_no_snapshot_returns_404(client):
    r = plan(client, "2026-01-01T00:00:00Z")
    assert r.status_code == 404
    assert "no recovery point" in r.json()["detail"]


def test_plan_overlap_gap_and_format_filter(client):
    register(
        client,
        snapshots=[snap("s1", "2026-01-01T00:00:00Z", 5)],
        segments=[
            seg("l1", "2026-01-01T00:00:00Z", 6, 9),    # replay 6..9
            seg("l2", "2026-01-01T00:00:00Z", 8, 12),   # overlap: replay 10..12 only
            seg("l3", "2026-01-01T00:00:00Z", 14, 15),  # gap at 13 -> chain stops
            seg("l4", "2026-01-01T00:00:00Z", 6, 99, fmt="wal-v2"),  # wrong format
        ],
    )
    r = plan(client, "2026-06-01T00:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot"]["id"] == "s1"
    assert body["replay"] == [
        {"id": "l1", "from_seq": 6, "to_seq": 9},
        {"id": "l2", "from_seq": 10, "to_seq": 12},
    ]
    assert body["endpoint"] == 12


def test_plan_without_segments_ends_at_applied_seq(client):
    register(client, snapshots=[snap("s1", "2026-01-01T00:00:00Z", 42)])
    body = plan(client, "2026-06-01T00:00:00Z").json()
    assert body["replay"] == []
    assert body["endpoint"] == 42


def test_visibility_cutoff_by_query_time(client):
    register(
        client,
        snapshots=[
            snap("old", "2026-01-01T00:00:00Z", 3),
            snap("new", "2026-03-01T00:00:00Z", 100),
        ],
        segments=[seg("l1", "2026-02-01T00:00:00Z", 4, 10)],
    )
    # before the newer snapshot and the segment exist
    body = plan(client, "2026-01-15T00:00:00Z").json()
    assert body["snapshot"]["id"] == "old"
    assert body["replay"] == []
    assert body["endpoint"] == 3
    # after everything: segment extends the old snapshot, but the newer
    # snapshot alone reaches further
    body = plan(client, "2026-04-01T00:00:00Z").json()
    assert body["snapshot"]["id"] == "new"
    assert body["endpoint"] == 100


def test_tiebreak_prefers_higher_applied_seq_then_smaller_id(client):
    register(
        client,
        snapshots=[
            snap("b", "2026-01-01T00:00:00Z", 10),
            snap("a", "2026-01-01T00:00:00Z", 10),  # same endpoint, smaller id wins
            snap("c", "2026-01-01T00:00:00Z", 8),
        ],
        segments=[seg("l1", "2026-01-01T00:00:00Z", 9, 20)],
    )
    # all snapshots can reach endpoint 20 (c: 9..20, a/b: 11..20);
    # higher applied_seq wins, then lexicographically smaller id
    body = plan(client, "2026-06-01T00:00:00Z").json()
    assert body["snapshot"]["id"] == "a"
    assert body["replay"] == [{"id": "l1", "from_seq": 11, "to_seq": 20}]
    assert body["endpoint"] == 20


def test_chain_tiebreak_fewest_segments_then_lexicographic(client):
    register(
        client,
        snapshots=[snap("s", "2026-01-01T00:00:00Z", 0)],
        segments=[
            seg("b", "2026-01-01T00:00:00Z", 1, 4),   # reaches further in one hop
            seg("a", "2026-01-01T00:00:00Z", 1, 3),   # ...but "a" sorts first
            seg("c", "2026-01-01T00:00:00Z", 3, 10),
        ],
    )
    # both [b, c] and [a, c] reach endpoint 10 in 2 segments;
    # the lexicographically smaller id sequence ["a", "c"] must win
    body = plan(client, "2026-06-01T00:00:00Z").json()
    assert [step["id"] for step in body["replay"]] == ["a", "c"]
    assert body["replay"] == [
        {"id": "a", "from_seq": 1, "to_seq": 3},
        {"id": "c", "from_seq": 4, "to_seq": 10},
    ]
    assert body["endpoint"] == 10


def test_chain_prefers_fewer_segments_over_lexicographic(client):
    register(
        client,
        snapshots=[snap("s", "2026-01-01T00:00:00Z", 0)],
        segments=[
            seg("z", "2026-01-01T00:00:00Z", 1, 10),  # one segment suffices
            seg("a", "2026-01-01T00:00:00Z", 1, 5),
            seg("b", "2026-01-01T00:00:00Z", 6, 10),
        ],
    )
    body = plan(client, "2026-06-01T00:00:00Z").json()
    assert [step["id"] for step in body["replay"]] == ["z"]
    assert body["endpoint"] == 10


def test_chain_skips_fully_covered_segments(client):
    register(
        client,
        snapshots=[snap("s", "2026-01-01T00:00:00Z", 0)],
        segments=[
            seg("wide", "2026-01-01T00:00:00Z", 1, 10),
            seg("inner", "2026-01-01T00:00:00Z", 3, 5),  # entirely inside "wide"
        ],
    )
    body = plan(client, "2026-06-01T00:00:00Z").json()
    assert [step["id"] for step in body["replay"]] == ["wide"]
    assert body["endpoint"] == 10


def test_timezone_offsets_and_naive_timestamps(client):
    register(
        client,
        # 2026-01-01T02:00:00+02:00 == 2026-01-01T00:00:00Z
        snapshots=[snap("s", "2026-01-01T02:00:00+02:00", 7)],
        segments=[seg("l", "2026-01-01T00:00:00Z", 8, 8)],
    )
    body = plan(client, "2026-01-01T00:00:00Z").json()  # naive -> treated as UTC
    assert body["endpoint"] == 8
    assert body["replay"] == [{"id": "l", "from_seq": 8, "to_seq": 8}]


def test_snapshot_created_after_query_time_invisible(client):
    register(client, snapshots=[snap("s", "2026-05-01T00:00:00Z", 1)])
    assert plan(client, "2026-04-01T00:00:00Z").status_code == 404
    assert plan(client, "2026-05-01T00:00:00Z").status_code == 200  # inclusive


def test_invalid_query_time_rejected(client):
    assert plan(client, "not-a-time").status_code == 422
