# Gujarat CCTV Integration Platform

Statewide CCTV integration built for the Gujarat Police Hackathon 2026. The
platform onboards heterogeneous cameras, runs ANPR continuously, traces a
vehicle's route across cameras, and alerts on watchlist matches in real time.

The design is a hybrid of the three integration models set out in the problem
statement: a registry control plane (Model 1), direct feed access (Model 2) and
an adapter spine (Model 3). It is edge first, meaning analytics run next to the
camera, only metadata crosses the network, and video is pulled on demand rather
than centrally recorded.

[Live demo](https://aalok.tail9e8ec5.ts.net) ·
[Presentation](dist/gujarat-cctv-deck.pdf) ·
[High level design](dist/gujarat-cctv-hld.pdf) ·
[Workflow diagram](dist/gujarat-cctv-workflow.pdf) ·
[Reader comparison](dist/gujarat-cctv-reader-comparison.pdf) ·
[API reference](https://aalok.tail9e8ec5.ts.net/api/docs)

![From camera to alert and trace](dist/gujarat-cctv-workflow.png)

## Contents

- [Measured results](#measured-results)
- [How this maps to the evaluation framework](#how-this-maps-to-the-evaluation-framework)
- [Scalability and PoC readiness](#scalability-and-poc-readiness)
- [The constraint the design turns on](#the-constraint-the-design-turns-on)
- [Capabilities](#capabilities)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Design rules](#design-rules)
- [Repository layout](#repository-layout)
- [Module reference](#module-reference)
- [Data model](#data-model)
- [Testing](#testing)
- [Known limits](#known-limits)
- [Security and privacy](#security-and-privacy)
- [Documentation](#documentation)
- [Licence](#licence)

## Measured results

| Metric | Target | Measured |
|---|---|---|
| Plate to route on the map | 2 s | 26 ms |
| Vehicle seen to alert on screen | 5 s | 1.04 s |
| Onboard a new camera | 30 s | 5.1 s |
| Plates read exactly, daylight test clips | not set | 86.6% |
| Plates read exactly, ANPR grade cameras | not set | 82.4% |
| Night plates found (Tesseract, docTR, PaddleOCR-VL) | not set | 35%, 62%, 100% |
| Network per camera, metadata only | not set | ~0.5 kbit/s |
| Automated tests | not set | 869 |

Accuracy is reported per scene condition rather than as a single average:
86.6% exact by day, 58.6% at night, 57.1% under glare. Averaging the three
would describe no camera on the estate.

## How this maps to the evaluation framework

| Criterion | Where the evidence is |
|---|---|
| 1. Successful test case | The 30 government cameras are onboarded from the grid's own catalogue (`make onboard`), viewable live, and analysed. The government-feed detection report lists every vehicle with time, camera, location, type, colour and evidence image. `GET /api/cameras/anpr-capability` grades each camera from measured crops |
| 2. Solution presentation | [dist/gujarat-cctv-deck.pdf](dist/gujarat-cctv-deck.pdf), editable as [.pptx](dist/gujarat-cctv-deck.pptx) |
| 3. Solution architecture | [dist/gujarat-cctv-hld.pdf](dist/gujarat-cctv-hld.pdf) ([source](docs/hld.md)) and the [workflow diagram](dist/gujarat-cctv-workflow.pdf) |
| 4. Working platform and demonstration | [Hosted platform](https://aalok.tail9e8ec5.ts.net) with a read-only evaluation account; the same stack runs locally with `docker compose up -d` |
| 5. Video analytics output | ANPR with per-track voting and positional normalisation; vehicle type and colour; CSV and PDF reports; accuracy reported per condition; [reader comparison](dist/gujarat-cctv-reader-comparison.pdf) |
| 6. Scalability and PoC readiness | [Scalability and PoC readiness](#scalability-and-poc-readiness) below; sharded workers need no orchestrator; `docker-compose.edge.yml` runs the analytics tier on its own |
| 7. Submission completeness | This README, the [API reference](https://aalok.tail9e8ec5.ts.net/api/docs), `.env.example` for every setting, and 869 automated tests |

| Bonus consideration | What is built |
|---|---|
| Hybrid architecture with operational value | Model 1 registry as control plane, Model 2 direct feed access, Model 3 adapters |
| Cross-camera tracking and correlation | Route trace across cameras with travel-time plausibility (cloned-plate signal); a vehicle id joined by plate or learned appearance for vehicles whose plate cannot be read |
| Analytics beyond ANPR | Vehicle type and colour, tamper detection, stream health grading, on-demand OCR boost; face detection ships disabled |
| Edge processing and low bandwidth | Analytics beside the camera, ~0.5 kbit/s of metadata per camera, video pulled only when someone watches |
| Security, privacy, audit, RBAC | Operator and viewer roles, append-only audit of every view, search and export, case references, no stored video |
| Dashboards, alerts, health, APIs | Performance page, alert console, health prober, OpenAPI for every endpoint |

## Scalability and PoC readiness

Measured on one 20-core box, then scaled by arithmetic. Every figure below comes
from [docs/hld.md](docs/hld.md) §7, where the method is stated.

| The framework asks for | This design |
|---|---|
| Central, regional and edge compute | Edge: one 20-core / 32 GB node per ~50 cameras (measured 50–57), beside the cameras. At 80,000 cameras that is ~1,600 nodes, ~48 per district. District: aggregation, relay and local console. State: registry, index, API and console at the SDC |
| GPU or accelerator requirements | None for the default path: the docTR reader runs on the CPU (37 ms per plate, ~30 busy cameras per reader). A GPU is needed only for the PaddleOCR-VL boost (~2.5 busy cameras per RTX A5000), capped at 12 cameras per request, so one GPU per region covers it. Cross-camera GPU batching is the named next step, plausibly taking 1,600 nodes toward ~400 |
| Network bandwidth and low-bandwidth strategy | ~40 Mbit/s statewide for metadata (0.5 kbit/s per camera, measured). On-demand video at 1% concurrent viewing adds ~2.4 Gbit/s. Central ingest would need ~240 Gbit/s |
| Hot, warm and cold storage | Video stays on departmental NVR/VMS (0 added centrally). Hot: 7 days of events, ~365 GB. Warm: 90 days, columnar, ~450 GB. Evidence crops: 7 days, ~10 TB. Compressed rows cost ~5 GB/day, so the index can be kept for years |
| Load balancing, horizontal scaling, monitoring, logging, health checks | Workers claim shards with a database advisory lock: add replicas to scale, and a crashed worker's shard is reclaimed with no orchestrator. Per-camera health probing, a live performance page, write-health counters and structured logs |
| High availability, backup, DR, cybersecurity | Built: shard failover, relay restarts with an `unstable` state, database reconnect and retry, credentials from the environment only, RBAC and audit. Designed: active-passive DR between the SDC and a second site, secret vault, mTLS, camera-VLAN segmentation |
| Implementation and operational cost | Quantities from the sizing: ~1,600 edge nodes, one storage server for crops, one GPU per region for the boost, ~40 Mbit/s of statewide backhaul, and no central video storage (a central VMS would need ~26 PB). Departments keep their existing cameras, NVRs, VMS and AMCs. Prices depend on procurement rates and are not quoted |

## The constraint the design turns on

The evaluation supplies a designated vehicle registration number partway
through the run. By the time that number is handed over the vehicle has already
passed the cameras, so there is no opportunity to go and look for it. The only
thing that can answer is an index of every plate already read from every
camera.

The platform therefore persists every ANPR read unconditionally from the moment
feeds go live, never only the watchlist matches. Every other decision in the
codebase follows from that one.

## Capabilities

| Capability | Summary |
|---|---|
| Camera registry | Single source of truth for cameras. CRUD, bulk CSV import, health probing, PostGIS coverage wedges, corridor gap analysis. No stream URL, credential or coordinate is hardcoded elsewhere. |
| Live view | Any camera, any protocol, playable in the browser in about 0.2 s. Video is pulled on demand by a separate relay and released 45 s after the last viewer disconnects. |
| ANPR pipeline | Decode, adaptive sampling, vehicle tracking, plate localisation, OCR, per track voting, positional normalisation, persist. Sustains 50 to 57 cameras per 20 core box. |
| Vehicle trace | `GET /api/vehicles/{plate}/journey` returns one GeoJSON response carrying the OSRM snapped route, the visits and per hop plausibility. |
| Live alerting | Watchlist matching on the sighting write path, in three tiers plus an appearance only tier. |
| Report export | CSV and PDF carrying plate, confidence, camera, geolocation, timestamp, vehicle class and the evidence crop. |
| Performance page | Live measured figures: stream count, fps, detections per minute, p95 query latency, uptime and OCR shed rate. |
| Live grid ingestion | Cameras, endpoints and stream properties are read from the grid catalogue at `/cameras.json` after sign in, never inferred. See [docs/integration-contract.md](docs/integration-contract.md). |
| Vehicle id | Every sighting carries a vehicle id, joined by a readable plate or by a learned appearance vector (DINOv2-small, pgvector), so a vehicle whose plate cannot be read can still be followed with `#id` in the trace. An appearance link is labelled as a lead to check, never an identification |
| On demand OCR boost | An operator can switch the cameras around a narrowed search area to PaddleOCR-VL on GPU for a set period, from the trace panel or `POST /api/ocr-boosts`. |
| Self escalating models | Cheap stages run by default. A camera the light path demonstrably cannot read escalates one stage at a time, under budget, and rolls back if the heavier stage does not help. |

## Quick start

Requires Docker with Compose, and roughly 12 GB of RAM for the full stack
including the ANPR tier.

```bash
cp .env.example .env          # set POSTGRES_PASSWORD, it has no default
make videos                   # generate test clips and the ground truth manifest
docker compose up -d          # database, media server, 50 simulated cameras, API, web
make migrate                  # apply migrations, forward only
make seed                     # cameras, corridor, watchlist, planted plates
```

The operator map is at <http://localhost:5173> and the API with its Swagger
page at <http://localhost:8000/api/docs>.

### Onboarding the evaluation grid

Point `SENTINEL_BASE` at the grid and reconcile the registry against its
catalogue:

```bash
make onboard-dry              # report what would change, write nothing
make onboard                  # reconcile the registry against /cameras.json
```

`onboard` is a reconciliation and is safe to re-run. Cameras that have appeared
are added, properties are refreshed, and cameras that have left the catalogue
are marked offline rather than deleted, because their sightings are evidence.

### Scaling the analytics tier

```bash
ANPR_DOCKERFILE=services/anpr/Dockerfile.gpu ANPR_SHARDS=6 \
  docker compose up -d --build --scale anpr=6 anpr
```

Each worker claims a shard through a Postgres advisory lock, so horizontal
scaling needs no orchestrator, no leader election and no per replica
configuration.

### Road snapped journeys

OSRM needs a one time extract of about 219 MB:

```bash
make osrm-prepare
docker compose --profile routing up -d osrm
```

Deployment beyond localhost is covered in [docs/deploy.md](docs/deploy.md).

## Architecture

```
cameras ──rtsp/http/hls/onvif──┐
                               ├── adapters ──┬── relay ──── MediaMTX ──── browser (WebRTC)
                               │              │              (pull on demand, 45 s idle)
                               │              └── ANPR workers ── sightings ─┬── trace  (historical query)
                               │                  (sharded, edge first)      └── alerts (streaming match)
                               └── health prober ── registry (PostGIS)
```

Component choices:

- **TimescaleDB with PostGIS** holds the registry, the geospatial data and the
  time series in one database, which avoids a distributed consistency problem
  this scale does not have.
- **MediaMTX** accepts RTSP and serves WebRTC.
- **OSRM** snaps journeys to the real Gujarat road network.
- **MapLibre GL** renders camera densities at the scale the design claims;
  Leaflet's DOM markers do not hold up at that count.

Retrospective trace and live alerting share the `sightings` table and no code.
A trace is a historical query triggered by an operator typing a plate. An alert
is a streaming match evaluated as each sighting is written. They are graded
separately, and building one on the assumption that it covers the other
delivers half the requirement.

## Design rules

### Invariants

These are numbered because the code cites them by number. Each exists because
breaking it produced a specific, recorded failure.

1. **Persist every plate read, not just watchlist matches.** The designated
   registration number arrives after the vehicle has already passed. Without an
   unconditional index the trace is impossible. This is the rule the product
   rests on.
2. **Retrospective trace and live alerting are separate code paths.** They share
   the `sightings` table and no code. Both are graded; neither implies the
   other.
3. **Plate normalisation is positional, at write time and at query time.**
   Indian plates have fixed structure (`GJ 01 AB 1234`: two letters, one or two
   digits, zero to three letters, four digits). Ambiguous OCR characters are
   coerced by slot, so a digit becomes a letter in a letter position and the
   reverse. A naive global map such as `O` to `0` everywhere collapses genuinely
   different plates onto one key. Use `normalise_plate()` in
   `services/common/plates.py`; never inline a variant.
4. **The registry is the only source of truth for cameras.** No hardcoded stream
   URL, credential or coordinate anywhere in application code or config.
5. **Never commit credentials.** Camera passwords, API keys and database URLs
   come from the environment only. A stream URL carrying embedded credentials is
   rejected with a 422.
6. **The catalogue decides which cameras exist.** Never count cameras, never key
   on a number.
7. **All timing comes from PTS**, never from frame arrival or the declared frame
   rate.
8. **A loop point is a scene discontinuity.** Long lived scene state retires on
   it.

### Conventions

- **Report confidence honestly.** A pipeline reporting 0.99 on a repeated
  misread is worse than one reporting 0.7 accurately. Confidence is the weakest
  character's probability, capped at 0.99 and lowered by disagreement between
  reads. Accuracy is reported per scene condition, never as a single average.
- **Aggregate OCR reads per tracked vehicle, not per frame.** One sighting per
  track, confidence weighted across the track's lifetime. Emitting per frame
  produces duplicate sightings and worse accuracy.
- **Reads failing the Indian plate format are kept, flagged and reported.** A
  missed wanted vehicle is worse than a noisy record.
- **Geographic responses are GeoJSON**, not ad hoc latitude and longitude
  objects, so the map layer and any GIS tool consume them directly.
- **Migrations are forward only and checksum verified.** A changed migration
  that has already been applied is an error, not a silent re-run.

## Repository layout

```
services/
  adapters/    camera types behind one interface, credentials resolved from env
  anpr/        the analytics pipeline, its OCR backends, and evidence crops
  alerting/    match tiers, watchlist cache, alert writer
  journey/     visits, hops, legs, plausibility scoring
  relay/       on demand video pull supervisor
  health/      probing and status rules
  reports/     CSV and PDF detection export
  analytics/   second analytics module, demonstrating the pipeline takes plugins
  api/         FastAPI application, the control and query surface
  common/      plate normalisation, OCR confusion model, gazetteer, geo, config
  migrate/     forward only migration runner
  simulator/   the 50 synthetic cameras used for load and acceptance
db/migrations/ forward only, checksum verified
data/          gazetteer and the derived OCR confusion table, both regenerable
web/src/       React and MapLibre operator UI
scripts/       onboarding sync, test video generation, acceptance tests, measurement
docs/          problem statement, integration contract, build plan, ANPR method
tests/         869 unit tests plus the acceptance harness
```

Two files under `data/` are derived rather than authored and can be
regenerated: `data/ocr-confusion.json` with `make confusion`, and the registry
itself with `make onboard`.

## Module reference

| Module | Responsibility | Worth knowing |
|---|---|---|
| `services/common` | Plate normalisation, OCR confusion table, gazetteer, geo helpers, config, audit | The confusion table is derived from measured reads, never hand typed. `geo.py` enforces a 120 km/h plausibility ceiling between hops. |
| `services/adapters` | One interface over RTSP, HTTP, HLS and ONVIF cameras | The catalogue client is deliberately strict: it names its failure modes rather than letting `json.load` raise. Credentials resolve from the environment by reference, never from a database row. |
| `services/health` | Liveness probing and status rules | Status is probed, not asserted from the last successful connection. |
| `services/relay` | Pulls a camera into MediaMTX on demand | Starts when an operator opens a camera, stops 45 s after the last reader. Only adapters reporting `needs_relay` route through it. |
| `services/anpr` | Frames to rows in `sightings` | The longest module. Bounds OCR and sheds work rather than queueing it, because a thread blocked in OCR has stopped decoding, and a starved decoder loses whole vehicles rather than one vehicle's vote. |
| `services/alerting` | Watchlist matching on the write path | A match is not a boolean. Tiers are confirmed, probable, possible and attribute, so both a certain and an uncertain match reach a human with an honest label. |
| `services/journey` | Historical reconstruction of a vehicle's route | Visits, hops, legs and per hop plausibility, OSRM snapped where routing is available. |
| `services/reports` | CSV and PDF export | Confidence is printed beside every plate. Hiding a 0.44 read among 0.95 ones invites exactly the over reading rule 6 exists to prevent. |
| `services/analytics` | Second analytics module | Exists to demonstrate that the pipeline accepts plugins rather than hardcoding ANPR. |
| `services/api` | FastAPI control and query surface | Every route carries a summary and description, because the generated Swagger page is itself a graded artifact. |
| `web/src` | React and MapLibre operator UI | Uncertainty is drawn rather than hidden. `VITE_API_BASE=""` is load bearing for the tunnelled build. |
| `scripts` | Onboarding, seeding, probing, measurement, acceptance | Source of the measured figures quoted above. |

## Data model

One database holds the registry, the geospatial data and the time series.

`sightings` is the table the design rests on. Every ANPR read lands there
unconditionally, with its plate, normalised key, confidence, camera, timestamp,
vehicle class and evidence crop reference.

Sightings carry no foreign key to a vehicle record, because a vehicle record
does not exist at read time and may never exist. The normalised plate key is
the join, and it is computed identically on write and on query.

Rows with no readable plate are still recorded, carrying vehicle attributes
only, so an appearance can be matched even when the registration cannot.
Every row also carries a `vehicle_uid`, with how it was joined (`plate`,
`appearance` or `new`) and the appearance distance, so a trace by vehicle id can
show which visits rest on a plate and which on a look.

Migrations are forward only and checksum verified. A changed migration that has
already been applied is an error, not a silent re-run.

## Testing

```bash
make test          # 869 unit tests
make lint          # ruff
make fmt           # ruff format
```

Each milestone has a binary acceptance test that is run rather than assumed:

```bash
make accept-m0   # 50 live RTSP endpoints, 50 registry rows, 50 map pins
make accept-m1   # a camera onboarded through the API turns green within 30 s
make accept-m2   # clicking any pin plays video within 3 s
make accept-m3   # 50+ streams processing, sightings filling, latency recorded
make accept-m4   # a planted plate traced across 3+ cameras within 2 s
make accept-m5   # a watchlisted plate raises an alert within 5 s, with evidence
make accept-m6   # a PDF downloads containing real detections with timestamps
make accept-m7   # the performance page renders live figures under 50 stream load
```

Unit tests run against the code directly. Acceptance tests run against the
actual stack and are the source of the figures in [Measured
results](#measured-results).

## Known limits

The platform reports what it cannot do, because a submission is easier to trust
when it does.

**All 30 government cameras fall below ANPR grade, and the platform says
which.** Of 80 cameras on the estate, `anpr-capability` grades 47 as ANPR grade
and 30 below it. The split is clean because the government feeds are wide area
situational awareness views: their plate crops average 66 px across, against
276 px on the simulated farm, or roughly 7 px per character. No recogniser
resolves a registration number at that scale. Rather than quoting an estate
wide accuracy figure true of no camera,
`GET /api/cameras/anpr-capability` grades every camera from crops the pipeline
actually measured, and accuracy is scoped to the cameras it holds for.

**Under load most OCR attempts are shed, and the figure is on the performance
page.** At 76 cameras on a 20 core box roughly 95% of attempts are shed. The
honest capacity for a box of that size is 50 to 57 cameras. The remedy is more
boxes, which is the edge first architecture working as designed rather than a
workaround.

**Accuracy is condition scoped.** See [Measured
results](#measured-results).

**On the government feeds, an appearance link re-finds a vehicle on one camera;
none has crossed cameras.** Of 43 links checked by eye, 34 were the same object,
7 were wrong and 2 were unclear; at the shipped threshold 16 of 17 were right.
Those cameras are kilometres apart and low resolution, and plates there are a
median 29 px wide, so no reader returns valid plates from them.

## Security and privacy

- Credentials resolve from the environment only. `.env.example` documents the
  full set and `.env` is gitignored. A stream URL containing embedded
  credentials is rejected with a 422.
- Every stream view, plate search, journey query and report export is audited
  with the actor and an optional case reference, in an append only log the
  application exposes no endpoint to rewrite.
- Two roles. `operator` reads and writes, `viewer` reads only. The evaluation
  account is a viewer, enforced by the API rather than by hiding buttons.
- The live view requests video only, never audio. These are public space
  cameras, and pulling audio nobody asked for would be a privacy decision made
  by accident.
- Crops, not frames. One evidence JPEG of roughly 8 kB per sighting. No full
  frames and no recorded video. Video stays at the camera and is fetched when a
  person asks for it.

## Documentation

| Document | Contents |
|---|---|
| [docs/problem-statement.md](docs/problem-statement.md) | What is being solved and what is graded |
| [docs/hld.md](docs/hld.md) | High level design, answering the eight required elements |
| [docs/integration-contract.md](docs/integration-contract.md) | The live grid contract. Read before touching anything that opens a stream |
| [docs/model-selection.md](docs/model-selection.md) | Model choices with the measurements behind them |
| [docs/anpr-method.md](docs/anpr-method.md) | The recognition pipeline in detail |
| [docs/reader-comparison.md](docs/reader-comparison.md) | Plate readers against device capacity, and when to use the OCR boost |
| [docs/field-observations.md](docs/field-observations.md) | What the real feeds actually look like |
| [docs/edge-placement.md](docs/edge-placement.md) | Where analytics run and why |
| [docs/build-plan.md](docs/build-plan.md) | Milestones and their acceptance criteria |
| [docs/deploy.md](docs/deploy.md) | Deployment beyond localhost |
| [docs/hosting.md](docs/hosting.md) | Hosting topology and tunnelling |
| [docs/deck.md](docs/deck.md) | Presentation content |

## Licence

Apache 2.0. See [LICENSE](LICENSE).
