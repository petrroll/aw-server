import random
from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def bucket(flask_client):
    "Context manager for creating and deleting a testing bucket"
    try:
        bucket_id = "test"
        r = flask_client.post(
            f"/api/0/buckets/{bucket_id}",
            json={"client": "test", "type": "test", "hostname": "test"},
        )
        assert r.status_code == 200
        yield bucket_id
    finally:
        r = flask_client.delete(f"/api/0/buckets/{bucket_id}")
        assert r.status_code == 200


def test_info(flask_client):
    r = flask_client.get("/api/0/info")
    assert r.status_code == 200
    assert r.json["testing"]
    assert "query.categorize_v2.v1" in r.json["capabilities"]
    assert "query.categorize_v2_explain.v1" in r.json["capabilities"]
    assert "query.active_periods_v2.v1" in r.json["capabilities"]
    assert "query.map_event_fields.v1" in r.json["capabilities"]
    assert (
        "query.merge_subwatcher_fields.source_namespace.v1"
        in r.json["capabilities"]
    )


def test_buckets(flask_client, bucket, benchmark):
    @benchmark
    def list_buckets():
        r = flask_client.get("/api/0/buckets/")
        print(r.json)
        assert r.status_code == 200
        assert len(r.json) == 1


def test_heartbeats(flask_client, bucket, benchmark):
    # FIXME: Currently tests using the memory storage method
    # TODO: Test with a longer data section and see if there's a significant difference
    # TODO: Test with a larger bucket and see if there's a significant difference
    @benchmark
    def heartbeat():
        now = datetime.now()
        r = flask_client.post(
            f"/api/0/buckets/{bucket}/heartbeat?pulsetime=1",
            json={"timestamp": now, "duration": 0, "data": {"random": random.random()}},
        )
        assert r.status_code == 200


def test_get_events(flask_client, bucket, benchmark):
    n_events = 100
    start_time = datetime.now() - timedelta(days=100)
    for i in range(n_events):
        now = start_time + timedelta(hours=i)
        r = flask_client.post(
            f"/api/0/buckets/{bucket}/heartbeat?pulsetime=0",
            json={"timestamp": now, "duration": 0, "data": {"random": random.random()}},
        )
        assert r.status_code == 200

    @benchmark
    def get_events():
        r = flask_client.get(f"/api/0/buckets/{bucket}/events")
        assert r.status_code == 200
        assert r.json
        assert len(r.json) == n_events

        r = flask_client.get(f"/api/0/buckets/{bucket}/events?limit=-1")
        assert r.status_code == 200
        assert r.json
        assert len(r.json) == n_events

        r = flask_client.get(f"/api/0/buckets/{bucket}/events?limit=10")
        assert r.status_code == 200
        assert r.json
        assert len(r.json) == 10

        r = flask_client.get(f"/api/0/buckets/{bucket}/events?limit=100")
        assert r.status_code == 200
        assert r.json
        assert len(r.json) == n_events

        r = flask_client.get(f"/api/0/buckets/{bucket}/events?limit=1000")
        assert r.status_code == 200
        assert r.json
        assert len(r.json) == n_events


# TODO: Add benchmark for basic AFK-filtering query


def test_query_invalid_timeperiod(flask_client):
    """Malformed timeperiods must yield 400 (client error), not 500.

    Regression test: a non-ISO8601 timeperiod previously raised an uncaught
    iso8601.ParseError, surfacing as an Internal Server Error.
    """
    r = flask_client.post(
        "/api/0/query/",
        json={"query": ["RETURN = 1;"], "timeperiods": ["not-a-valid-period"]},
    )
    assert r.status_code == 400
    assert "not-a-valid-period" in r.json["message"]


def test_query_timeperiod_missing_slash(flask_client):
    """A timeperiod without a start/end slash separator must yield 400, not 500.

    Regression test: a single ISO8601 datetime (no slash) previously raised an
    uncaught IndexError when indexing the split result.
    """
    r = flask_client.post(
        "/api/0/query/",
        json={"query": ["RETURN = 1;"], "timeperiods": ["2024-01-01T00:00:00+00:00"]},
    )
    assert r.status_code == 400


def test_query_valid_timeperiod(flask_client):
    """A well-formed query with a valid timeperiod still succeeds."""
    r = flask_client.post(
        "/api/0/query/",
        json={
            "query": ["RETURN = 1;"],
            "timeperiods": ["2024-01-01T00:00:00+00:00/2024-01-02T00:00:00+00:00"],
        },
    )
    assert r.status_code == 200
    assert r.json == [1]
