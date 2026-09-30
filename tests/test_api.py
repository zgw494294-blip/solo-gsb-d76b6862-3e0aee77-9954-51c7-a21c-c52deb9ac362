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


# ---------------------------------------------------------------- cleanup


def cleanup(client, ids, moments):
    return client.post(
        "/cleanup", json={"ids": list(ids), "moments": list(moments)}
    )


def test_cleanup_unused_items_deleted(client):
    register(
        client,
        snapshots=[
            snap("old", "2026-01-01T00:00:00Z", 5),
            snap("new", "2026-07-01T00:00:00Z", 100),
        ],
        segments=[seg("seg-a", "2026-02-01T00:00:00Z", 6, 50)],
    )
    # at the late moment the newest snapshot alone reaches 100; seg-a and the
    # old snapshot are irrelevant there and may be removed
    r = cleanup(
        client,
        ["seg-a", "old"],
        ["2026-08-01T00:00:00Z"],
    )
    assert r.status_code == 200
    assert r.json() == {
        "deleted_ids": ["seg-a", "old"],
        "endpoints": [{"at": "2026-08-01T00:00:00+00:00", "endpoint": 100}],
    }
    # deletion is real: at the earlier moment only the old snapshot existed,
    # so there is no recovery point anymore
    assert plan(client, "2026-03-01T00:00:00Z").status_code == 404
    body = plan(client, "2026-08-01T00:00:00Z").json()
    assert body["snapshot"]["id"] == "new"
    assert body["endpoint"] == 100


def test_cleanup_blocked_when_any_moment_decreases_and_nothing_deleted(client):
    register(
        client,
        snapshots=[
            snap("old", "2026-01-01T00:00:00Z", 5),
            snap("new", "2026-07-01T00:00:00Z", 100),
        ],
        segments=[seg("seg-a", "2026-02-01T00:00:00Z", 6, 50)],
    )
    # early moment: endpoint 50 via old + seg-a; late moment: endpoint 100.
    # deleting "new" is harmless early but drops the late moment 100 -> 50.
    r = cleanup(
        client,
        ["new"],
        ["2026-03-01T00:00:00Z", "2026-08-01T00:00:00Z"],
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["decreased"] == [
        {"at": "2026-08-01T00:00:00+00:00", "before": 100, "after": 50}
    ]
    # atomicity: nothing was deleted
    body = plan(client, "2026-08-01T00:00:00Z").json()
    assert body["snapshot"]["id"] == "new"
    assert body["endpoint"] == 100
    body = plan(client, "2026-03-01T00:00:00Z").json()
    assert body["endpoint"] == 50


def test_cleanup_blocked_reports_every_decreased_moment(client):
    register(
        client,
        snapshots=[
            snap("old", "2026-01-01T00:00:00Z", 5),
            snap("new", "2026-07-01T00:00:00Z", 100),
        ],
        segments=[seg("seg-a", "2026-02-01T00:00:00Z", 6, 50)],
    )
    # deleting both snapshots removes the recovery point at both moments:
    # 50 -> null and 100 -> null; null ranks below every sequence number
    r = cleanup(
        client,
        ["old", "new"],
        ["2026-03-01T00:00:00Z", "2026-08-01T00:00:00Z"],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["decreased"] == [
        {"at": "2026-03-01T00:00:00+00:00", "before": 50, "after": None},
        {"at": "2026-08-01T00:00:00+00:00", "before": 100, "after": None},
    ]
    # nothing deleted
    assert plan(client, "2026-08-01T00:00:00Z").json()["endpoint"] == 100


def test_cleanup_null_to_null_is_not_a_decrease(client):
    register(
        client,
        snapshots=[snap("s1", "2026-01-01T00:00:00Z", 7)],
        segments=[seg("l1", "2026-02-01T00:00:00Z", 8, 20)],
    )
    # guard moments where deleting l1 changes nothing visible:
    # 2020: null before and after (null is allowed to stay null);
    # 2026-01-15: l1 does not exist yet, endpoint is 7 either way
    r = cleanup(
        client,
        ["l1"],
        ["2020-01-01T00:00:00Z", "2026-01-15T00:00:00Z"],
    )
    assert r.status_code == 200
    assert r.json()["endpoints"] == [
        {"at": "2020-01-01T00:00:00+00:00", "endpoint": None},
        {"at": "2026-01-15T00:00:00+00:00", "endpoint": 7},
    ]
    # the segment is really gone
    assert plan(client, "2026-03-01T00:00:00Z").json()["endpoint"] == 7


def test_cleanup_may_remove_original_snapshot_and_replay_chain(client):
    register(
        client,
        snapshots=[
            snap("a", "2026-01-01T00:00:00Z", 0),
            snap("b", "2026-01-01T00:00:00Z", 10),   # original winner (higher applied_seq)
        ],
        segments=[
            seg("sa", "2026-01-01T00:00:00Z", 1, 10),  # chain winner (smaller id)
            seg("sb", "2026-01-01T00:00:00Z", 1, 10),
        ],
    )
    # before: snapshot "b" alone reaches 10; "a" + chain also reaches 10
    assert plan(client, "2026-06-01T00:00:00Z").json()["snapshot"]["id"] == "b"
    # delete the original winning snapshot AND the original winning chain
    # segment: "a" + "sb" still reaches 10, so the endpoint does not drop
    r = cleanup(client, ["b", "sa"], ["2026-06-01T00:00:00Z"])
    assert r.status_code == 200
    assert r.json()["endpoints"] == [
        {"at": "2026-06-01T00:00:00+00:00", "endpoint": 10}
    ]
    body = plan(client, "2026-06-01T00:00:00Z").json()
    assert body["snapshot"]["id"] == "a"
    assert body["replay"] == [{"id": "sb", "from_seq": 1, "to_seq": 10}]
    assert body["endpoint"] == 10


def test_cleanup_unknown_id_rejects_whole_request_without_partial_delete(client):
    register(
        client,
        snapshots=[snap("s1", "2026-01-01T00:00:00Z", 7)],
        segments=[seg("l1", "2026-02-01T00:00:00Z", 8, 20)],
    )
    r = cleanup(
        client,
        ["l1", "ghost"],
        ["2026-06-01T00:00:00Z"],
    )
    assert r.status_code == 409
    assert "ghost" in r.json()["detail"]
    # the valid id in the same request was not deleted either
    assert plan(client, "2026-06-01T00:00:00Z").json()["endpoint"] == 20


@pytest.mark.parametrize(
    "payload",
    [
        {"ids": [], "moments": ["2026-06-01T00:00:00Z"]},                 # no ids
        {"ids": ["s1"], "moments": []},                                   # no moments
        {"ids": [""], "moments": ["2026-06-01T00:00:00Z"]},               # empty id
        {"ids": ["a", "a"], "moments": ["2026-06-01T00:00:00Z"]},         # duplicate id
        {"ids": ["a"] * 101, "moments": ["2026-06-01T00:00:00Z"]},        # >100 ids
        {"ids": ["a"], "moments": ["2026-06-01T00:00:00Z"] * 9},          # >8 moments
        {"ids": ["a"], "moments": ["not-a-time"]},                        # bad moment
        {"ids": ["a"], "moments": ["2026-06-01T00:00:00Z"], "x": 1},      # unknown field
    ],
)
def test_cleanup_invalid_request_rejected(client, payload):
    assert client.post("/cleanup", json=payload).status_code == 422


def test_cleanup_accepts_up_to_100_ids_and_preserves_order(client):
    segments = [seg(f"l{i}", "2026-01-01T00:00:00Z", i, i) for i in range(1, 101)]
    assert register(client, segments=segments).status_code == 201
    # no snapshots at all: endpoint null at the guard moment before and after
    r = cleanup(client, [f"l{i}" for i in range(1, 101)], ["2026-06-01T00:00:00Z"])
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_ids"] == [f"l{i}" for i in range(1, 101)]
    assert body["endpoints"] == [
        {"at": "2026-06-01T00:00:00+00:00", "endpoint": None}
    ]
    # they are really gone: repeating the request now reports unknown ids
    r2 = cleanup(client, ["l1"], ["2026-06-01T00:00:00Z"])
    assert r2.status_code == 409


def test_cleanup_duplicate_moments_allowed_and_echoed_in_order(client):
    register(client, snapshots=[snap("s1", "2026-01-01T00:00:00Z", 7)])
    r = cleanup(
        client,
        ["s1"],
        ["2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"],
    )
    assert r.status_code == 200
    assert r.json()["endpoints"] == [
        {"at": "2020-01-01T00:00:00+00:00", "endpoint": None},
        {"at": "2020-01-01T00:00:00+00:00", "endpoint": None},
    ]


def test_cleanup_normalizes_moment_timezone_offsets(client):
    register(client, snapshots=[snap("s1", "2026-01-01T02:00:00+02:00", 7)])
    # == 2026-01-01T00:00:00Z, snapshot visible (inclusive boundary):
    # deleting it drops endpoint 7 -> null, so the request must be blocked
    r = cleanup(client, ["s1"], ["2026-01-01T02:00:00+02:00"])
    assert r.status_code == 409
    assert r.json()["detail"]["decreased"] == [
        {"at": "2026-01-01T00:00:00+00:00", "before": 7, "after": None}
    ]
    assert plan(client, "2026-01-01T00:00:00Z").status_code == 200
