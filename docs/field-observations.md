# Field observations — real evaluation feeds

Source: screenshots of the actual camera feeds available for testing/evaluation,
supplied 2026-08-18. This file exists because several of these observations
contradict assumptions baked into the original build plan, and a fresh session
must not re-derive them from scratch.

Cameras seen: `Camera 1 / 01 Chiman bhai Bridge / CSITMS-32_PTZ2` (Ahmedabad),
`Camera 8 / 08 majewadi-gate-junagadh / Majevadi Gate PTZ-2` (Junagadh),
`Camera 31 / Bhavnath Mandir FIX-3` (Junagadh/Girnar), `Camera 23 / 30 kheram`.

---

## 1. The estate is statewide and scattered, not a corridor

Ahmedabad and Junagadh are ~350 km apart, on opposite sides of the state.
Bhavnath Mandir is at Girnar. There is no single highway corridor connecting the
supplied cameras.

**Consequence.** A vehicle trace across these cameras will be *sparse* — a
handful of sightings hundreds of kilometres apart, not a dense chain. The
journey endpoint must render convincingly from 2–3 widely separated points, and
the OSRM leg between them will be long. The "≥ 3 sightings" acceptance figure in
M4 stands, but it will come from the simulated NH-48 farm; the real feeds should
be treated as a second, sparser scenario.

The seeded NH-48 corridor farm remains the right test harness — it is the only
way to exercise dense multi-camera tracing — but the registry must not assume
corridor topology anywhere.

## 2. Cameras are a mix of PTZ and fixed, and PTZ ones move

Device labels carry the type: `CSITMS-32_PTZ2`, `Majevadi Gate PTZ-2` are
pan-tilt-zoom; `Bhavnath Mandir FIX-3` is fixed.

**Consequence.** `bearing`, `fov_degrees` and `range_m` are only stable for
fixed cameras. For a PTZ they describe the current preset, not the camera. The
registry needs to record which kind a camera is, and coverage/gap analysis (M1)
must either use the PTZ's home preset or mark its coverage as variable. Treating
a PTZ's bearing as ground truth would produce confidently wrong coverage
polygons.

## 3. Burnt-in camera clocks are wrong, and disagree with each other

Camera 1 overlay reads `14-06-2026 00:54:39`; Camera 8 reads `14-06-2026
00:58:25`; Camera 31 reads `2026-08-10 10:35:27`. The actual wall-clock date at
capture was **18 Aug 2026**. So two cameras are ~2 months slow, a third ~8 days
slow, and they do not agree with one another.

**Consequence — this is the most operationally important observation here.**

- `sightings.ts` must be the **ingest timestamp**, never the burnt-in overlay.
- Cross-camera journey reconstruction depends entirely on comparable
  timestamps. If we had trusted the overlay, every journey would be nonsense and
  the speed-plausibility check would fire constantly.
- Clock skew is worth surfacing: a camera whose stream time drifts from platform
  time is a health signal that belongs next to `camera_health`.

## 4. Night and headlight glare are the dominant condition

Three of four frames are night scenes. Camera 1 and Camera 8 both show severe
headlight bloom — large saturated white regions that wash out everything behind
them. In Camera 8 an entire approaching vehicle is lost inside the glare.

**Consequence.** The original "one deliberately awful stream" plan understates
the problem: glare is not the exception, it is the normal case. The synthetic
clips need a night/glare profile, and ANPR accuracy must be reported per
condition rather than as a single headline number. The honesty convention, "report
confidence honestly rather than claiming an accuracy figure you cannot defend",
is doing real work here.

## 5. Many cameras never see a vehicle

Bhavnath Mandir looks at a pedestrian market stall. Camera 23 looks down an
empty residential lane. Neither will produce plate reads in any volume.

**Consequence.** Sightings will be heavily skewed toward a few road-facing
cameras. Any per-camera throughput metric must not treat "zero detections" as a
fault — the health prober needs to distinguish *no stream* from *no traffic*.

## 6. Overlays are burnt into the frame and will be OCR'd as plates

Every feed has burnt-in text: a timestamp band, a camera name, `REC`, and a
site label in a large font. Some of it is high-contrast white-on-dark text of a
similar size to a plate.

**Consequence.** Without masking, the OCR stage will happily return `CSITMS-32`
or `14-06-2026` as plate candidates. Two mitigations, both cheap:

- Only run OCR inside a detected *vehicle* box, never on the whole frame.
- Keep the positional format check — `14-06-2026` does not normalise to a valid
  mark — but remember that invalid reads are *persisted and flagged*, so junk
  overlay reads would otherwise accumulate in `sightings` as noise.

Overlay regions are stable per camera, so a per-camera ignore-region in the
registry is the durable fix.

---

## Implications logged against milestones

| Milestone | Change |
|---|---|
| M1 | Registry needs `camera_kind` (ptz/fixed) and optional OCR ignore regions; coverage analysis must special-case PTZ |
| M3 | `sightings.ts` = ingest time, explicitly not overlay time; OCR confined to vehicle boxes; night/glare must be in the test set |
| M3/M7 | Report ANPR accuracy split by day/night/glare, not as one number |
| M1/M7 | Clock-skew detection as a camera health signal |
| M4 | Journey must render well from 2–3 sparse, distant sightings, not just dense chains |

---

# Organiser FAQ — facts that change the build

Source: hackathon FAQ page, supplied 2026-08-18.

## Dataset and streaming

- **~12 hours of CCTV footage from each of 50 cameras**, across five government
  departments: **Health, Police, GSRTC, Panchayat, Municipal Corporation**. The
  seeded farm now uses these five department names.
- Recorded footage is served as **simulated live video** by a **Python streaming
  middleware** that **synchronises all cameras onto a common timeline** and
  exposes **one streaming endpoint URL per camera**.
- Simulated rather than production streams, for security and so every team is
  tested on an identical, repeatable dataset.

Three consequences:

1. **12-hour clips dissolve the loop-period problem.** Our own constraint — a
   looped clip only yields a clean N-camera journey if its duration exceeds
   N x inter-camera travel time — is comfortably satisfied by the real feeds.
   It still binds for our synthetic harness, where clips are short.
2. **A synchronised common timeline is exactly what cross-camera tracing
   needs**, and it confirms the decision to timestamp on ingest rather than
   from the burnt-in overlay, which §3 above shows is wrong by weeks.
3. **One URL per camera maps straight onto the registry.** Swapping the
   simulated farm for the real feeds is an update to `cameras.stream_ref`, which
   is the payoff for routing everything through the registry (invariant 4).

## Evaluation areas (all seven are scored)

1. Successful test case — onboarding + analytics on the government feed
2. Solution presentation — clarity, model justification
3. Solution architecture — technical soundness, HLD quality
4. Working platform — maturity of the demonstrated software
5. Video analytics output — quality of ANPR, detection, reports
6. **Scalability and PoC readiness — ~80,000-camera readiness**
7. Submission completeness

**Area 6 is the one that most changes the plan.** The sizing narrative must be
written against ~80,000 cameras, not the 50 in the demo. That is a ~1,600x
multiple, and it makes the edge-first architecture the load-bearing argument:
only metadata crosses the network, video is pulled on demand. Concretely, the
HLD and the M7 performance page need per-camera resource figures measured on the
demo farm and extrapolated honestly, with the bottleneck named.

## Bonus

Bonus consideration exists but **will not compensate for non-compliance with
mandatory requirements** — so M9 stays strictly after M0-M8, exactly as the
build plan sequences it.

Named bonus areas, several of which the current architecture already covers:
hybrid architectures, advanced cross-camera tracking, additional reliable
analytics, edge processing, bandwidth optimisation, enhanced
cybersecurity/auditability, operational dashboards, automated alerts, health
monitoring, integration-ready APIs.

---

# Live integration test against the real feeds (2026-08-18)

The 31 evaluation cameras at `live.sentinelgujarat.in` were onboarded into the
registry and probed by the platform. This was run as a deliberate test of the
project's central architectural claim — that swapping simulated feeds for real
ones is a registry change and nothing else. Findings below are from the live
system, not from documentation.

## What the feeds actually are

`GET /api/cameras/{n}/state` returns, per camera:

```json
{"id":"2","number":2,"name":"Camera 2","location":"02 Janpath",
 "codec":"h264","container":"mp4","status":"live","delivery":"progressive",
 "stream_url":"/stream/2","hls_url":null,
 "timezone":"Asia/Kolkata","drift_tolerance":5.0,
 "slot_offset":4978.57,"slot_seconds":43200.0,"loop":true,
 "wall_time":"2026-08-18T10:22:58+05:30","server_epoch":1787028778.57}
```

- **Delivery is progressive HTTP byte-range, not RTSP and not HLS.** `hls_url`
  is null on all 31 cameras. The page loads hls.js but sets `video.src` to a
  plain URL and the server answers `206 Partial Content`. Our `adapter_type`
  enum had no honest value for this, so migration 003 adds `http`; calling it
  `hls` would have sent a client looking for a playlist that does not exist.
- **No authentication.** Both the state API and the streams answer
  unauthenticated, so no credential handling is needed for the evaluation.
  `credential_ref` stays in the schema for real deployments.
- **Containers vary: 25 mp4, 4 mkv, 2 avi.** Codec is h264 throughout.
- **The estate is statewide**, spanning 14 districts — Ahmedabad, Gandhinagar,
  Junagadh, Gir Somnath, Rajkot, Kutch, Patan, Banaskantha, Navsari, Kheda.
  This confirms §1 above from the live data rather than from screenshots.
- Display numbering has gaps (…23, 28, 30, 33–38), so the upstream index is not
  the estate's own camera number. We key on our own `external_ref`.

## The common timeline — this solves the clock problem

`slot_seconds` is 43200: a **12-hour** recording per camera, matching the
organiser FAQ. `loop` is true and `slot_offset` reports how far into that slot
the middleware currently is. Every camera reported the same offset (±1 s) when
polled together, so the feeds are genuinely **synchronised onto one timeline**.

The server also publishes `wall_time` and `server_epoch`.

Taken with §3 above — burnt-in camera clocks wrong by weeks and disagreeing with
each other — the timestamp policy is now settled and evidence-based:

1. `sightings.ts` = **our ingest time**. Never the burnt-in overlay.
2. `server_epoch` gives an authoritative reference to measure our own drift
   against, and `drift_tolerance` (5 s) is the budget the middleware expects.
3. `slot_offset` is worth recording alongside a sighting, because it locates the
   detection in the *footage*, which is what makes a result reproducible when an
   evaluator replays the same 12-hour slot.

## Two cameras are dead, and the upstream does not admit it

Cameras 6 (`06 Timbavadi gate-Junagadh`) and 22 (`28 BK Mervada tran Rasta`)
return **HTTP 500 on every request** — not the transient 503 seen during normal
stream warm-up, but a persistent failure. Both are the two `avi`-container
cameras, so the middleware most likely cannot remux AVI into a progressive
response.

**Both report `"status": "live"` in the state API.**

This is the strongest justification for the platform running its own health
prober rather than mirroring an upstream status field. Our prober marks them
`offline` with the reason `upstream error HTTP 500`, which is the true state.
29 of 31 government cameras serve video.

## What the integration test cost in code

The claim held up. Onboarding 31 real cameras required:

- one migration (`003_real_feeds.sql`) adding the `http` adapter and `camera_kind`;
- one seed script that reads the upstream state API and writes to the registry;
- **two genuine bug fixes in the prober**, both found by real feeds and neither
  reachable with the simulated farm:
  1. `probe_http` issued a plain GET. Against a progressive endpoint that never
     returns, so the first real camera would have hung the prober indefinitely.
     Now a `Range: bytes=0-0` request.
  2. Probing ran serially. 81 cameras took 19 s against a 5 s interval, which
     breaks "a new camera turns green quickly" and scales nowhere near 80,000
     cameras. Direct probes now fan out across a thread pool: 19 s → 5.2 s.

No adapter code, no API endpoint, no map code and no schema table changed. The
map renders all 81 cameras — simulated and real — from the same GeoJSON.
