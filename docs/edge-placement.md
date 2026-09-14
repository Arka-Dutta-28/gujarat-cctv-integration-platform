# The edge node, in two placements

> **The claim:** the analytics unit is *one container image* that runs unchanged
> on a Jetson-class device in a junction cabinet, on a district server, or in the
> state data centre — and scaling it is a count, not a redesign.
>
> This document is how that claim is demonstrated rather than asserted. It is
> one of the six named bonus criteria, and the one most easily waved at.

---

## Why it matters

The honest counterargument a sharp evaluator will raise:

> *"You are proposing edge hardware you cannot deploy for this demo."*

True. There is no hardware at the test cameras' sites, so demo analytics run on
our own machine pulling those streams. The mitigation has to be real rather than
rhetorical, and it is this: **run the same image in two placements at once,
publishing to the same store**, and show that nothing distinguishes them but an
environment variable.

---

## How the two placements coordinate — which is to say, they don't

There is **no orchestrator, no leader election, and no per-node configuration.**

Each worker claims the lowest free shard with a **Postgres advisory lock**:

```
    worker starts
      └─ SELECT pg_try_advisory_lock(ns, 0)   taken
      └─ SELECT pg_try_advisory_lock(ns, 1)   taken
      └─ SELECT pg_try_advisory_lock(ns, 2)   → got it. This is shard 2 of N.
```

A camera belongs to shard `hash(camera_id) % N`, which every worker computes
identically without asking anyone.

Two properties fall out for free, and both matter at 1,600 nodes:

- **A crashed worker returns its shard the instant its connection drops.** The
  lock is held by the *session*, so there is no timeout to tune, no heartbeat to
  miss and no reaper to write.
- **A node in a junction cabinet and a replica in the data centre coordinate
  through the database they already share.** Nothing new is deployed to make
  distribution work.

> This was earned, not designed in. An earlier version sharded by container
> hostname — which Compose sets to a random hex id — and six replicas took
> shards 0, 0, 3, 3, 3, 4. A third of the estate was processed three times over
> while two shards were never processed at all, and **nothing reported an
> error.**

---

## Demonstrating it

### Verified locally, 31 Aug 2026

Three workers, one machine, no configuration distinguishing them:

```
$ ANPR_SHARDS=3 docker compose up -d --scale anpr=3

cctv-anpr-2   shard 0 of 3   27 cameras
cctv-anpr-1   shard 1 of 3   27 cameras
cctv-anpr-3   shard 2 of 3   27 cameras
```

Distinct shards, an 81-camera estate split evenly, claimed in whatever order the
containers happened to start.

### Across two machines

`docker-compose.edge.yml` runs **only** the ANPR worker — no database, no API,
no media server, no simulator. Those are the centre.

On the second machine:

```bash
DATABASE_URL=postgresql://user:pass@central-host:5432/cctv \
ANPR_SHARDS=4 \
docker compose -f docker-compose.edge.yml up -d
```

Set `ANPR_SHARDS` to the total across **both** machines. Then check from the
centre that every shard is claimed exactly once and that sightings are arriving
from cameras the second machine owns:

```sql
SELECT camera_id, count(*), max(ts)
  FROM sightings
 WHERE ts > now() - interval '5 min'
 GROUP BY camera_id ORDER BY 3 DESC LIMIT 10;
```

### What to say on camera

Two sentences, because the point is small and easily over-explained:

> *"This second machine is running the identical container image. It was given a
> database address and a worker count, nothing else — it worked out which
> cameras it owns by itself, and those sightings are arriving in the same index."*

---

## What crosses the link

This is the edge-first argument in one table, and it is why the second placement
is cheap:

| | Per camera | × 80,000 |
|---|---|---|
| Raw H.264 video | ~3 Mbit/s | 240 Gbit/s |
| **What actually crosses** | **~0.5 kbit/s** | **~40 Mbit/s** |

Evidence crops are written to local disk and *referenced* by the sighting, so a
thumbnail only crosses the network when somebody opens it.

---

## Honest limits

- **This demonstrates distribution, not edge hardware.** Both placements in the
  local demonstration are ordinary machines. What is proven is that the unit is
  portable and self-coordinating, which is the part that would otherwise be a
  claim.
- **`ANPR_SHARDS` must agree across every machine.** It is the one number that
  is not derived, and setting it too low leaves cameras unprocessed. A worker
  that finds no free shard exits rather than idling, so the mistake is visible
  in a restart loop rather than silent.
- **Store-and-forward is not built.** A true edge node should buffer sightings
  through a WAN outage and replay them on reconnect. It is specified in
  `docs/hld.md` §1.5 and named as a rollout prerequisite in §8.2; it is not
  implemented, and this file does not pretend otherwise.
