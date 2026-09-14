# Build Plan — Test Case & Submission
## Gujarat Police Hackathon 2026 · Integrated Video Management & Analytics Platform

> Read alongside the design rules in `README.md`. Each milestone below ends in something demonstrable, with an acceptance test that is run rather than assumed.

---

## 1. What the test scenario actually demands

Three consequences that are not obvious from a first reading, and that change what gets built.

### 1.1 The plate number arrives *during* evaluation — so the index must already exist

> *"During the evaluation, participants will be provided with a designated vehicle registration number."*

By the time you receive it, the vehicle has already passed the cameras. There is no opportunity to "go look for it." The only way to answer is a **query against an index of every plate already read from every camera**.

This kills the natural optimisation of storing only watchlist hits. Every ANPR read must be persisted unconditionally, from the moment the feeds go live. If the platform has been ingesting for two hours before the evaluator hands over the number, you have two hours of searchable history. If it starts when they hand it over, you have nothing.

**Operational consequence:** start ingestion as early as the evaluation format permits, and make "system has been running since HH:MM, N sightings indexed" visible on screen. It demonstrates the design rather than just claiming it.

### 1.2 Two capabilities, two code paths

| | Retrospective trace | Live watchlist alerting |
|---|---|---|
| Trigger | Operator enters a plate | Each new sighting written |
| Data | Historical `sightings` index | Streaming match against loaded watchlist |
| Latency target | < 2 s query | < 5 s detection to alert |
| Expected Output bullet | 1 and 2 | 3 |

Teams that build one will assume it covers the other. It does not. Both are separately named in Expected Output.

### 1.3 "End-to-end system performance" is an artifact, not a claim

> *"Evidence of successful CCTV integration, AI-powered video analytics, interoperability, scalability, and end-to-end system performance."*

Same pattern as the output report hiding inside Deliverable 4: this sentence requires a **metrics surface**. Streams active, frames processed per second, detection rate, queue depth, p95 journey-query latency, uptime. Build it as a page in the platform — then it serves the test case, the demo videos, and evaluation criterion 06 simultaneously.

---

## 2. Test harness — simulating 50 cameras from your video files

You cannot test the central feature without multi-camera sightings of the same vehicle. This harness is therefore **milestone zero**, not a side tool.

### Design

1. **Loop video files as RTSP endpoints.** MediaMTX publishes each source file on a continuous loop at `rtsp://mediamtx:8554/cam-01` … `cam-50`. To the adapter layer these are indistinguishable from real cameras — which means the adapter code is genuinely exercised, not stubbed.

2. **Assign real Gujarat geography.** Seed the 50 cameras along an actual road corridor (the NH-48 Ahmedabad–Vadodara–Surat stretch works well) with real coordinates, bearings and FOV. OSRM then snaps journeys to a real road network instead of drawing straight lines across farmland.

3. **Offset playback per camera.** Start `cam-02` 90 seconds behind `cam-01`, `cam-03` 210 seconds behind, and so on. The same vehicle in the same clip now appears at successive cameras at successive times along a plausible route — a synthetic but geometrically valid journey.

4. **Plant known plates.** Record or select clips containing plates you control. Seed some into the watchlist (alerting path) and keep others out (trace path). Both test paths become deterministic and repeatable.

5. **Vary the sources.** Mix resolutions, frame rates, codecs and at least one deliberately awful stream — the real cameras will be heterogeneous and it is better to discover your pipeline's failure modes now.

> When the ~50 government feeds appear on the Resources page, this becomes a configuration change: swap simulated stream URLs for real ones in the registry. Nothing else moves. That is the payoff for making the file source a real adapter rather than a special case.

---

## 3. Data model

Full DDL in `db/schema.sql`. The parts that matter:

**`cameras`** — the control plane. `id`, `name`, `department`, `adapter_type`, `stream_ref`, `geom` (PostGIS Point), `bearing`, `fov_degrees`, `range_m`, `status`, `last_seen`, `retention_days`, `ownership_type`, `amc_expiry`.

**`sightings`** — Timescale hypertable, partitioned on `ts`. `camera_id`, `plate_raw`, `plate_normalised`, `confidence`, `ts`, `track_id`, `vehicle_class`, `colour`, `bbox`, `crop_path`, `format_valid`.

> Indexes here decide whether you pass the live demo. `(plate_normalised, ts DESC)` is the journey query. A trigram index on `plate_normalised` handles partial and fuzzy search. GIST on `cameras.geom` for spatial queries.

**`watchlist`** — `plate`, `plate_normalised`, `category`, `severity`, `source`, `case_ref`, `active`.

**`alerts`** — `sighting_id`, `watchlist_id`, `match_tier`, `priority`, `status`, `acknowledged_by`, `acknowledged_at`.

**`audit_log`** — every stream view, plate search and export. Required for the security narrative and cheap to add early; painful to retrofit.

---

## 4. The endpoint that wins the test case

```
GET /api/vehicles/{plate}/journey?from=&to=
```

Returns a GeoJSON `FeatureCollection`:
- one `LineString` — the OSRM-snapped route
- one `Point` per sighting — `ts`, `camera_id`, `camera_name`, `confidence`, `thumbnail_url`
- `properties.segments[]` — per-hop distance, elapsed time, implied speed, plausibility flag
- `properties.confidence` — overall, degraded by implausible transitions

This single response drives the map render, the timeline and the exportable movement history. Build it as one endpoint rather than assembling the view from three calls — under live demo conditions, fewer round trips is fewer failure modes.

---

## 5. Milestones

Each has a binary acceptance test. Do not advance until it passes.

### M0 · Foundation and camera farm
Docker Compose: TimescaleDB+PostGIS, Redpanda, MediaMTX, OSRM, API, web. Feed simulator looping local videos to 50 RTSP endpoints. Seed script placing 50 cameras along NH-48 with staggered offsets.
**Accept:** `docker compose up` gives 50 live RTSP endpoints, 50 registry rows, 50 pins on the map.

### M1 · Registry and GIS *(mandatory Model 1)*
Camera CRUD, bulk CSV import, API onboarding, GeoJSON layer endpoint, health prober, coverage and gap analysis.
**Accept:** a camera can be added through the UI in under 30 seconds and turns green. Gap analysis returns uncovered segments.

### M2 · Adapter framework and live view
`CameraAdapter` interface with `rtsp`, `file`, `hls`, `onvif` implementations. Credential handling out of the registry. MediaMTX proxy to WebRTC.
**Accept:** clicking any pin plays live video in under 3 seconds. Adding a new adapter type requires no changes outside its own module.

### M3 · ANPR pipeline
Decode, adaptive sampling, vehicle detect, ByteTrack, plate detect, rectify, OCR, per-track vote, positional normalise, persist **every** read.
**Accept:** 50 streams processing concurrently; `sightings` filling; measured throughput and per-stage latency logged.

> Instrument this milestone as you build it. The numbers become the performance evidence in M7 and the sizing figures in the HLD. Retrofitting instrumentation is wasted work.

### M4 · Search and journey reconstruction — **the test case**
Plate search with fuzzy fallback. Journey endpoint with OSRM snapping and plausibility scoring. Map route render with timestamped sighting markers and thumbnails.
**Accept:** entering a planted plate returns a route with ≥ 3 timestamped sightings in under 2 seconds, rendered on the map with a movement-history table.

### M5 · Watchlist and live alerting
Watchlist CRUD and bulk import. Streaming matcher on the sighting write path with the three match tiers. WebSocket push. Alert console with evidence card, acknowledge and dismiss.
**Accept:** adding a plate to the watchlist causes the next sighting of it to raise an alert in the UI within 5 seconds, with crop, camera, time and map pin attached.

### M6 · Report export *(Deliverable 4)*
`GET /api/reports/detections` → CSV and PDF: plate, confidence, camera, geolocation, timestamp, vehicle class, thumbnail.
**Accept:** a PDF downloads containing real detections with corresponding timestamps.

### M7 · Performance evidence *(Expected Output 4)*
Metrics collection and a performance page: streams active, FPS, detections/min, queue depth, p95 query latency, uptime, sightings indexed.
**Accept:** the page renders live figures under 50-stream load and is screen-recordable.

### M8 · Deploy and submission hygiene
Hosted instance, read-only demo account with seeded data, live Swagger page, README, licence, credential-free git history.
**Accept:** a logged-out incognito browser can reach the hosted platform, log in with the demo account, and open Swagger.

### M9 · Bonus — only after M0–M8 pass
Vehicle re-ID embeddings via `pgvector`, edge-node demonstration on separate hardware publishing to the same bus, camera tamper detection, FRS module scaffold (synthetic faces only — see the DPDP position in the submission plan).

---

## 6. Sequencing change from the earlier plan

Journey reconstruction (M4) now comes **before** watchlist alerting (M5). Previously reversed.

Reason: Expected Output lists the trace first, it is the harder of the two, and it is the one that cannot be faked under live conditions. Watchlist matching is roughly a day's work once `sightings` is flowing, because it is a streaming query over data that already exists. Building the hard, load-bearing thing first leaves the compressible task at the end where schedule pressure lands.

---

## 7. Known technical risks

| Risk | Mitigation |
|---|---|
| 50 concurrent streams exceed available GPU | Adaptive sampling to 2–3 fps, batch inference across streams, horizontal worker scaling. **Measure early in M3** — do not discover this in week four |
| Government feed formats unknown until Resources page opens | The adapter framework is the mitigation. Keep the `file` adapter working as a fallback demo path |
| ANPR weak at night, oblique angles, on two-wheelers | Per-track voting; report confidence honestly rather than claiming an accuracy figure you cannot defend |
| OSRM route snapping fails where cameras are far from mapped roads | Fall back to great-circle with a flag on the segment; never fail the whole journey response |
| Journey query slow under load | The `(plate_normalised, ts DESC)` index plus Timescale chunk exclusion. Benchmark at M4 with a populated table, not an empty one |
