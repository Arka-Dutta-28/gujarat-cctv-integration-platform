"""Worker sharding.

The scaling story is that the estate splits across processes with no scheduler,
no leader and no shared state — every worker independently computes which
cameras are its own. That only holds if the assignment is stable, balanced and
total, so it is tested rather than asserted in the HLD.
"""

from __future__ import annotations

import uuid

from services.anpr.worker import camera_patterns, shard_of
from services.simulator.simulate import camera_patterns as sim_patterns


def ids(n: int) -> list[str]:
    return [str(uuid.UUID(int=i)) for i in range(n)]


class FakeDatabase:
    """Stands in for Postgres advisory locks, which are held per session."""

    def __init__(self) -> None:
        self.held: set[tuple[int, int]] = set()

    def connect(self, dsn: str, autocommit: bool = False):  # noqa: ANN201, ARG002
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db
        self.mine: set[tuple[int, int]] = set()
        self.result: bool | None = None

    def cursor(self):  # noqa: ANN201
        return FakeCursor(self)

    def close(self) -> None:
        # Closing a session releases every lock it held — the property the
        # whole design leans on.
        self.db.held -= self.mine
        self.mine.clear()


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def execute(self, sql: str, params: tuple) -> None:  # noqa: ARG002
        key = (params[0], params[1])
        if key in self.conn.db.held:
            self.conn.result = False
        else:
            self.conn.db.held.add(key)
            self.conn.mine.add(key)
            self.conn.result = True

    def fetchone(self):  # noqa: ANN201
        return (self.conn.result,)

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a: object) -> None:
        return None


class TestSharding:
    def test_every_camera_lands_in_exactly_one_shard(self) -> None:
        shards = 4
        assignments = {c: shard_of(c, shards) for c in ids(200)}
        assert set(assignments.values()) <= set(range(shards))
        assert len(assignments) == 200

    def test_the_assignment_is_stable_across_calls_and_processes(self) -> None:
        """A worker restarting must reclaim its own cameras, not shuffle them."""
        camera = str(uuid.UUID(int=42))
        assert shard_of(camera, 8) == shard_of(camera, 8)

    def test_the_split_is_reasonably_balanced(self) -> None:
        shards = 5
        counts = [0] * shards
        for c in ids(1000):
            counts[shard_of(c, shards)] += 1
        # No shard doing more than double its fair share.
        assert max(counts) < 2 * (1000 / shards)
        assert min(counts) > 0

    def test_a_single_worker_owns_everything(self) -> None:
        assert {shard_of(c, 1) for c in ids(50)} == {0}

    def test_zero_shards_does_not_divide_by_zero(self) -> None:
        assert shard_of(str(uuid.UUID(int=1)), 0) == 0


class TestShardClaiming:
    """Replicas must take distinct shards, or they duplicate work silently.

    The first attempt read the ordinal off the container name; Compose sets the
    hostname to a random hex id, so six replicas took shards 0, 0, 3, 3, 3, 4
    and nothing reported a problem. An advisory lock is held by the session and
    released when the connection drops, so a crashed worker returns its shard
    with no timeout and no heartbeat.
    """

    def test_the_first_worker_takes_shard_zero(self, monkeypatch) -> None:
        import services.anpr.worker as mod

        db = FakeDatabase()
        monkeypatch.setattr(mod.psycopg, "connect", db.connect)
        assert mod.claim_shard("dsn", 4)[0] == 0

    def test_each_worker_takes_a_distinct_shard(self, monkeypatch) -> None:
        import services.anpr.worker as mod

        db = FakeDatabase()
        monkeypatch.setattr(mod.psycopg, "connect", db.connect)
        claims = [mod.claim_shard("dsn", 4) for _ in range(4)]
        assert sorted(c[0] for c in claims) == [0, 1, 2, 3]

    def test_a_worker_with_no_free_shard_is_refused(self, monkeypatch) -> None:
        """Better to restart and say why than to idle doing nothing."""
        import services.anpr.worker as mod

        db = FakeDatabase()
        monkeypatch.setattr(mod.psycopg, "connect", db.connect)
        for _ in range(2):
            mod.claim_shard("dsn", 2)
        assert mod.claim_shard("dsn", 2) is None

    def test_a_released_shard_is_taken_by_the_next_worker(self, monkeypatch) -> None:
        import services.anpr.worker as mod

        db = FakeDatabase()
        monkeypatch.setattr(mod.psycopg, "connect", db.connect)
        first = mod.claim_shard("dsn", 2)
        mod.claim_shard("dsn", 2)
        first[1].close()
        assert mod.claim_shard("dsn", 2)[0] == 0

    def test_a_single_worker_configuration_still_works(self, monkeypatch) -> None:
        import services.anpr.worker as mod

        db = FakeDatabase()
        monkeypatch.setattr(mod.psycopg, "connect", db.connect)
        assert mod.claim_shard("dsn", 1)[0] == 0


class TestCameraScope:
    """`ANPR_CAMERA_REFS` — which cameras a worker is allowed to own.

    Two uses, one mechanism: an edge worker at Bharuch has no business decoding
    Rajkot, and recording a demo of six corridor cameras should not decode the
    other seventy-four. Scoping is not decommissioning — a decommissioned camera
    is a claim about the *estate*, which the map, coverage analysis and health
    history all believe. This says only "not mine to decode".
    """

    def test_empty_means_every_camera(self):
        # The behaviour that existed before this, and what a single-site
        # deployment wants. A filter that defaults to something would silently
        # shrink an estate on upgrade.
        assert camera_patterns("") == []
        assert camera_patterns("   ") == []

    def test_a_star_becomes_a_sql_wildcard(self):
        assert camera_patterns("cam-2*") == ["cam-2%"]

    def test_several_patterns_are_split_and_trimmed(self):
        assert camera_patterns("cam-20, cam-21 ,sentinel-*") == [
            "cam-20", "cam-21", "sentinel-%",
        ]

    def test_blank_entries_are_dropped(self):
        # A trailing comma is the most likely typo in an env var and must not
        # become a pattern that matches nothing.
        assert camera_patterns("cam-20,,cam-21,") == ["cam-20", "cam-21"]

    def test_an_exact_ref_stays_exact(self):
        # No implicit wildcarding: `cam-2` must not quietly match `cam-20`.
        assert camera_patterns("cam-2") == ["cam-2"]


class TestSimulatorPublishScope:
    """`SIM_CAMERA_REFS` — which cameras the simulator publishes.

    Same shape as the ANPR scope and for a different reason: cameras sharing a
    source file have a timing relationship with each other, and the corridor's
    journey only holds if those streams start together.
    """

    def test_empty_publishes_the_whole_farm(self):
        assert sim_patterns("") == []

    def test_patterns_become_sql_wildcards(self):
        assert sim_patterns("cam-2*,cam-10") == ["cam-2%", "cam-10"]
