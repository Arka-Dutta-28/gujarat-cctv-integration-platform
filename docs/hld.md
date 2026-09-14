# Technical Proposal — High-Level Design

## Edge-First Federated Video Intelligence Platform
### Gujarat Police Hackathon 2026 — Integrated Video Management & Analytics Platform

> **Architecture class:** Hybrid — **Model 1** registry as control plane,
> **Model 2** direct feed access as the demonstrated path, **Model 3** adapter
> federation as the structural spine, delivering **Model 4**'s analytics
> outcomes through distributed edge inference rather than centralised video
> ingest.

---

## Executive summary

*One page. Every figure below is measured on the running system; §0 onwards
gives the method and the caveats.*

**The problem.** Gujarat has roughly 80,000 cameras across 26 departments, in
incompatible systems nobody can query together. The evaluation makes the
difficulty concrete: a registration number is handed over *during* the
demonstration, for a vehicle that has **already driven past**. Nothing can be
gone back for.

**The answer that follows from that.** Store every plate the estate reads, not
just the ones on a watchlist. This moves the system's cost from the query path
to the write path, and every sizing decision in this document follows from it. A
statewide trace then costs **26 ms**; the index that makes it possible is where
the engineering lives.

**How it is built.** Analytics run **next to the camera**; only metadata crosses
the network; video is pulled to the centre **only when a human asks to watch**.
A camera's row in the registry — not a config file — decides how it is reached,
who may see it, and where its coverage falls. That is a hybrid of the brief's
Model 1 registry, Model 2 direct feed access and Model 3 adapter federation, and
it delivers Model 4's analytics outcomes without Model 4's central video ingest.

**Why not central ingest.** At 80,000 cameras it needs **240 Gbit/s** of
sustained backhaul and **~26 PB** for fifteen days. Edge-first needs **~40
Mbit/s** and **~5 GB/day**, keeps working when the WAN drops, and preserves the
departments' existing VMS and storage investments rather than replacing them.
The arithmetic is in §7 and the per-camera figure it rests on — ~0.5 kbit/s — is
measured, not assumed.

**What was actually built and measured.**

| | measured | budget |
|---|---|---|
| Cameras onboarded | **80** (50 simulated, 30 government) | — |
| Statewide vehicle trace | **26 ms** p50 · 31 ms p95 | 2 s |
| Detection → alert raised | **1.04 s** | 5 s |
| Camera onboarding | **5.1 s** | 30 s |
| Concurrent streams held | **50 for 15 minutes**, 0 restarts | 50 |
| Throughput | **104 sightings/minute** across 71 cameras | — |
| Plate accuracy, ANPR-grade cameras | **82.4%** exact · 95.1% within 2 characters | — |
| Automated tests | **869** | — |

**What it does not claim.** All 30 government cameras fall below ANPR grade —
their plate crops average 66 px, about 7 px per character — and the platform
grades this itself rather than being told. Night accuracy is **58.6%** against
daylight's 86.6%. Edge autonomy, push-notification routing, mTLS and the vendor
VMS adapters are **designed and not built**, and are labelled as such throughout.
Vehicles whose plates cannot be read are still recorded by description, which
extends a trace but can never start one. §9 is the full list.

Saying that first is what makes the rest of the numbers worth reading.

---

## How to read this document

Every figure here is one of three things, and each is labelled:

| Mark | Meaning |
|---|---|
| **measured** | Observed on the 81-camera estate this platform was built and run against. Traceable to `scripts/acceptance/`, which carries the method |
| **arithmetic** | Derived from a measured figure by a calculation shown in the text, so it can be checked and disagreed with |
| **design** | Specified for statewide rollout and not yet built. Named as such, never implied to be working |

There are no unattributed numbers in this document, and no figure appears that
was estimated before the build and has since been measured.

### Traceability — the eight required HLD elements

| # | Required element | Section |
|---|---|---|
| 1 | Architecture, diagrams, component interactions | [§1](#1-architecture-and-component-interactions) |
| 2 | Integrating heterogeneous cameras, NVRs and VMS | [§2](#2-integrating-heterogeneous-cameras-nvrs-and-vms) |
| 3 | Ingesting and processing streams from dispersed locations | [§3](#3-ingesting-and-processing-streams-from-dispersed-locations) |
| 4 | Watchlist integration and continuous correlation | [§4](#4-watchlist-integration-and-continuous-correlation) |
| 5 | AI analytics — ANPR, FRS, object detection, person and vehicle tracking | [§5](#5-ai-analytics) |
| 6 | Alert generation and notification workflow | [§6](#6-alert-generation-and-notification-workflow) |
| 7 | Scalability, interoperability, security, performance at 80,000 cameras | [§7](#7-scalability-interoperability-security-and-performance-at-80000-cameras) |
| 8 | Prerequisites, assumptions, department information requirements | [§8](#8-prerequisites-assumptions-and-what-departments-must-supply) |

[§9](#9-what-this-platform-does-not-claim) states the limits, because a design
document that only lists strengths cannot be checked.

---

## 0. The constraint that shapes everything

The evaluation hands over a registration number *during* the demonstration, for
a vehicle that has already driven past the cameras. There is no opportunity to
go and look for it.

The only thing that can answer is **an index of every plate already read**.

That single requirement rules out the natural optimisation — storing only
watchlist hits — and it moves the system's load-bearing element from the query
path to the write path. A trace is a millisecond query against an index;
building and holding that index is where all of the cost lives. Every sizing
decision in §7 follows from it.

---

## 1. Architecture and component interactions

### 1.1 Design principles

Each traces to a stated requirement rather than to preference.

| # | Principle | Traces to |
|---|---|---|
| **P1** | **Video stays put; metadata moves.** Raw streams remain on departmental infrastructure. Only events cross the WAN by default | Core Goal: cost-effective, maximum reuse of existing infrastructure |
| **P2** | **The registry is the control plane.** No component hard-codes a camera. Cameras, credentials, adapters and geography all resolve through it | Model 1 mandatory; "onboarded without major redesign" |
| **P3** | **Every source is a plugin.** A bare RTSP camera and a Genetec CMS enter through one normalising adapter interface | Model 3; vendor neutrality; heterogeneity |
| **P4** | **Degrade, never fail.** WAN loss must not blind a district | Geographic dispersion (~1,000 km); bonus: low-connectivity operation |
| **P5** | **Open protocols at every seam.** ONVIF, RTSP, WebRTC, Kafka wire protocol, OpenAPI, GeoJSON. No proprietary interface between our own layers | Vendor-neutral, no lock-in, technology-agnostic |
| **P6** | **Privacy by default.** Metadata-first minimises what is centralised at all; retention tiered and enforced | DPDP Act 2023; bonus: privacy, auditability |

**P2 is not a slogan, and it has been tested.** On 31 August 2026 the
organisers' entire evaluation grid moved to a different hostname. Because no
component reconstructs a stream URL and every camera resolves through the
registry, that migration cost **one environment variable** and no code change.

### 1.2 Layered architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  L5  PRESENTATION      Command Centre · GIS Console · Alert Desk ·            │
│                        Journey Explorer · Registry Portal · Performance       │
├──────────────────────────────────────────────────────────────────────────────┤
│  L4  INTELLIGENCE      Watchlist Matcher · Journey Reconstruction ·           │
│                        Cross-Camera Re-ID · Alert Writer · Report Export      │
├──────────────────────────────────────────────────────────────────────────────┤
│  L3  PLATFORM CORE     Event Store · CAMERA REGISTRY & GIS ★ · IAM/RBAC ·     │
│                        Audit · Stream Broker (on-demand) · Event Bus          │
├──────────────────────────────────────────────────────────────────────────────┤
│  L2  FEDERATION        Adapter Framework · Credential Resolution ·            │
│                        Health Prober · Protocol Normalisation · Catalogue     │
├──────────────────────────────────────────────────────────────────────────────┤
│  L1  EDGE ANALYTICS    Decode · Sample · Detect · Track · Locate · OCR ·      │
│                        Vote · Tamper · Store-and-Forward                      │
├──────────────────────────────────────────────────────────────────────────────┤
│  L0  SOURCES           IP cams · Analog+encoder · NVRs · Departmental VMS ·   │
│                        Public-facing cameras (where permitted)                │
└──────────────────────────────────────────────────────────────────────────────┘
                    ★ = mandatory Model 1, positioned as the control plane
```

**Deployment tiers:** edge (police station or junction cabinet) → district
aggregation (33 districts) → state (SDC command centre + DR site).

### 1.3 Deployment boundary — what crosses which link

```
   district / corridor node                    state data centre
 ┌──────────────────────────┐               ┌───────────────────────┐
 │ cameras ── adapters      │               │  registry (PostGIS)   │
 │            │             │   metadata    │  sightings (Timescale)│
 │            ├─ ANPR ──────┼──────────────►│  watchlist / alerts   │
 │            │  (50/node)  │  ~0.5 kbit/s  │  audit trail          │
 │            │             │   per camera  │                       │
 │            └─ relay ─────┼──────────────►│  API · UI · OSRM      │
 └──────────────────────────┘  video, only  └───────────────────────┘
                               on demand
```

- **Northbound is metadata only.** Video crosses this boundary only when a
  person asks for it — and then as **one** pull serving both the operator and
  analytics, not two.
- **The registry is the only source of truth for cameras.** Nothing else holds
  a stream URL, credential or coordinate.
- **Credentials never enter the registry.** A stream URL carrying embedded
  credentials is rejected with HTTP 422; secrets resolve from the environment
  through an opaque `credential_ref`.

### 1.4 Component interaction — camera onboarding

```
 Operator      API        Registry     Catalogue     Gazetteer    Prober    Adapter
   │            │            │            │             │           │          │
   │ onboard ──►│            │            │             │           │          │
   │            │─── read catalogue ─────►│             │           │          │
   │            │◄── every camera, every endpoint,      │           │          │
   │            │    codec, resolution, declared rate ──│           │          │
   │            │            │            │             │           │          │
   │            │── position from place name ──────────►│           │          │
   │            │◄── lat/lon + precision (survey│landmark│city|district)      │
   │            │            │            │             │           │          │
   │            │── resolve adapter by source shape ───────────────────────►   │
   │            │◄────────────────── adapter class + needs_relay ──────────    │
   │            │            │            │             │           │          │
   │            │─ UPSERT ──►│  external_ref keyed; a surveyed position is     │
   │            │            │  never overwritten by a derived one             │
   │            │            │            │             │           │          │
   │            │──────────── probe transport ─────────────────────►│          │
   │            │◄─────────── online │ offline │ unstable │ unknown │          │
   │            │            │            │             │           │          │
   │◄─ camera on the map, health coloured, ready to stream ─────────│          │
   │            │            │            │             │           │          │
   │            │  Cameras absent from the catalogue are marked offline and    │
   │            │  KEPT — sightings reference them, and those rows are evidence│
```

**measured: 5.1 s** from request to a camera live on the map, against a 30-second
demonstration budget.

Two properties of this flow are deliberate and are the reason it survives a
changing estate:

- **Nothing enumerates cameras by counting.** The upstream catalogue is the
  contract; `/api/cameras/1..N` is not. Counting silently onboards the wrong
  cameras after a renumber *and reports success*, which is worse than failing.
- **Onboarding is reconciliation, not import.** It can be re-run at any time and
  converges the registry on the catalogue.

### 1.5 Component interaction — detection to alert, including WAN loss

```
 Camera    Edge node                         State DC              Operator
   │           │                                 │                     │
   │ frames ──►│ decode (PTS clock, never arrival time)                │
   │           │  │                              │                     │
   │           │  ├─ sample: rate follows the scene                    │
   │           │  ├─ detect + track vehicle → stable track_id          │
   │           │  ├─ locate plate INSIDE the vehicle box               │
   │           │  ├─ OCR each sampled crop                             │
   │           │  └─ vote across the whole track ── ONE plate string   │
   │           │                                 │                     │
   │           │═══ WAN UP ═══════════════════►  │                     │
   │           │   sighting (~300 B)             │                     │
   │           │                          ┌──────┴──────┐              │
   │           │                          │ ONE TRANSACTION            │
   │           │                          │  INSERT sighting           │
   │           │                          │  match vs watchlist cache  │
   │           │                          │  INSERT alert (if hit)     │
   │           │                          │  COMMIT — or neither row   │
   │           │                          └──────┬──────┘              │
   │           │                                 │──── alert ────────► │
   │           │                                 │   measured 1.04 s   │
   │           │                                 │   budget 5 s        │
   │           │                                 │                     │
   │           │╳╳╳ WAN DOWN ╳╳╳                 │                     │
   │ frames ──►│ detection CONTINUES                                   │
   │           │  └─ sightings to a bounded on-disk buffer   [design]  │
   │           │  └─ local watchlist cache still fires        [design] │
   │           │═══ RECONNECT ════════════════►  │                     │
   │           │   buffered sightings replayed, ordered by PTS         │
```

**The single-transaction property is the design's answer to two problems at
once.** TimescaleDB cannot enforce a foreign key *referencing* a hypertable, so
`alerts → sightings` integrity cannot be a database constraint. Writing both
rows in one transaction makes it structural instead: an alert can never point at
a sighting that does not exist. And because the match happens on the write path
rather than in a downstream consumer, the latency budget is settled by
construction rather than by tuning a queue.

> **Status honesty.** The WAN-down branch is **design**. Edge autonomy — the
> on-disk buffer and the local watchlist cache — is specified here and is not
> built; the platform was developed against feeds that reach it over the public
> internet, where the failure it protects against does not arise. The store-and-
> forward path is named in §8 as a rollout prerequisite, not claimed as working.

---

## 2. Integrating heterogeneous cameras, NVRs and VMS

### 2.1 One interface, and a rule that keeps it honest

Every source — a bare RTSP camera, an analogue camera behind an encoder, an NVR,
a departmental VMS — is reduced to the same question: *how do I obtain a
playable stream for this camera?*

```
CameraAdapter
  ├── discover()          → [CameraDescriptor]   ONVIF WS-Discovery, VMS API
  │                                              enumeration, catalogue read, CSV
  ├── describe(id)        → CameraDescriptor     geo, model, codec, resolution
  ├── open_stream(id)     → StreamDescriptor     URL + auth context + protocol
  ├── probe_health(id)    → HealthStatus         reachable, last-frame age, tamper
  └── fetch_clip(id,t0,t1)→ ClipRef              where the source supports it
```

> **The rule:** adding a new source type must require **no change outside its own
> module**. No `if adapter == …` branch anywhere else, no factory to edit, no new
> case in the API. Adapters self-register by decoration, and the registry's
> `adapter` column selects one by name.

That is not an aspiration — it is an acceptance test that runs against the
built system. It is the concrete answer to *"onboarded without major redesign."*

| Adapter | Status |
|---|---|
| Generic RTSP | **built** |
| HLS | **built** |
| Progressive HTTP / HTTPS | **built** |
| WHEP / WebRTC | **built** |
| File (recorded evidence, test clips) | **built** |
| ONVIF Profile S/T | **design** — the descriptor and discovery contract exist; no ONVIF device was available to test against |
| Vendor SDK (Hikvision ISAPI, Dahua, Axis VAPIX) | **design** |
| VMS API (Milestone MIP, Genetec) | **design** |

The honest position on the last three: they are *shaped* by the interface above
and are new classes rather than migrations, but no team should claim a working
Genetec federation without a Genetec instance to federate. §8 lists what a
department must supply for each to be built and tested.

### 2.2 The catalogue is the contract; the URL pattern is not

Where an upstream publishes a machine-readable camera list, onboarding reads
**that** and nothing else. It never enumerates by counting, never reconstructs a
stream URL from a template, and never keys a lookup table on a camera number.

This was validated the hard way. The organisers' catalogue moved host mid-build,
behind an HTTP 301. Because the parser is shape-tolerant — it accepts several
spellings of each field and classifies URLs by their shape rather than by the
key they arrived under — and because relative endpoints are resolved against the
host that *answered* rather than the one that was *asked*, the migration was
absorbed without a schema or code change.

> The failure this avoids is the dangerous kind: a system that enumerates by
> counting does not crash after a renumber. It onboards the wrong cameras and
> reports success.

### 2.3 Positions, and being honest about them

Most catalogues supply a place name and no coordinates. The platform is entirely
geospatial — coverage polygons, gap analysis, corridor search and journey
plausibility all need a position — so names are resolved through a gazetteer
keyed by **place name**, never by camera id.

Every position carries its own precision, and the map draws it to scale:

| Precision | Meaning | Rendered as |
|---|---|---|
| `survey` | Physically confirmed | Solid pin |
| `landmark` | ±250 m | Hollow pin, 250 m circle |
| `city` | ±3 km | Hollow pin, 3 km circle |
| `district` | ±15 km | Hollow pin, 15 km circle |
| `unplaced` | Unknown | Hollow pin, no circle |

A surveyed position, once entered by an operator, outranks anything the system
derives and is never overwritten by re-onboarding.

**Why this is in an HLD at all:** coverage and route plausibility are computed
*from* these positions. A derived pin rendered identically to a surveyed one
silently overstates every conclusion drawn from it. Showing the uncertainty is
what makes the coverage analysis in §7 defensible.

### 2.4 Credentials

`cameras.credential_ref` holds an opaque **key**, never a secret. The key
resolves to a username and password at connect time, from the process
environment.

The indirection is the control. `stream_ref` and `credential_ref` travel freely
through database dumps, API responses, GeoJSON properties, audit rows and log
lines; a secret that exists only in the environment cannot leak through any of
them. A stream URL submitted with credentials embedded is **rejected with HTTP
422** rather than stored.

For rollout, this module's backend is replaced by a managed vault with rotation
— **design** — and no adapter changes.

---

## 3. Ingesting and processing streams from dispersed locations

### 3.1 Where the processing happens, and why there

Centralising video does not close at 80,000 cameras, and the arithmetic is not
close:

| | Per camera | × 80,000 |
|---|---|---|
| H.264 CCTV stream, 1080p | ~3 Mbit/s | **240 Gbit/s** |
| ANPR metadata, this platform | ~0.5 kbit/s | **~40 Mbit/s** |

The second row is **measured**: the estate writes **104 sightings/minute across
71 contributing cameras**, and a sighting is a few hundred bytes.

That is a **four order of magnitude** reduction in what the network must carry,
and it is the difference between a statewide backbone programme and a set of
ordinary district links. It is also why departmental NVR, VMS, storage and AMC
investments stay in place rather than being replaced.

### 3.2 Video moves only when a person asks

Video is never centrally recorded. A relay starts an ffmpeg process when an
operator opens a camera and reaps it 45 seconds after the last reader
disconnects.

| | |
|---|---|
| Click to playing video, cold camera | **measured: 0.23 s** |
| Cost of a camera needing transcode for the browser | **measured: 78% of one core** |

That cost is **per viewer, not per camera**, which is the entire point: an
un-watched camera costs nothing beyond its metadata.

**Analytics never read the relayed stream.** The pipeline goes to the source
directly. Reading the browser-facing copy would place transcode artefacts
between the recogniser and the plate, and would make accuracy depend on how many
people happened to be watching.

### 3.3 Timing comes from the video, never from arrival

This is the subtlest requirement in the whole ingest path, and getting it wrong
is silent.

A gateway replays its buffered group-of-pictures on connect, so the first second
or two of frames arrive **faster than real time**. Timestamped by arrival, a
vehicle appears to cross a junction in tens of milliseconds; the journey
plausibility check in §4 then computes an implied speed far above its threshold
and — correctly, by its own rules — reports a **cloned plate**.

> Every reconnect would manufacture false criminal intelligence, on every
> camera, indefinitely.

All timing therefore derives from presentation timestamps carried by the video
itself. Exactly one module in the platform is permitted to open a video stream,
so the rule cannot be bypassed elsewhere, and the declared frame rate is read
for reporting only — never for timing.

### 3.4 Scene discontinuity

A looping recording, a camera reset and a relay restart are all the same event:
the scene stops being continuous.

At a discontinuity, open tracks are **completed and written** — never discarded,
because the index must contain every read — and tracker ids, the tamper
reference image and the motion baseline are all cleared. Carrying state across
the boundary would stitch the last vehicle before it to the first vehicle after,
which is precisely the input that produces a false clone report.

### 3.5 Dispersion and autonomy

| Tier | Runs | Survives |
|---|---|---|
| Edge node | Decode, detect, track, OCR, vote, tamper | District outage — **design** (buffer not built) |
| District | Aggregation, relay, local console | State outage — **design** |
| State | Registry, index, watchlist, alerts, API, UI | — |

The unit of deployment is **one container image**, identical on a Jetson-class
device, a district server or a demonstration machine. That is what makes the
placement claim testable rather than rhetorical: the same artifact runs in both
positions and publishes to the same store.

---

## 4. Watchlist integration and continuous correlation

### 4.1 Matching happens on the write path

Every sighting is matched against every active watchlist entry **as it is
written**, not by a consumer polling for new rows.

The watchlist is therefore held in process and refreshed on a timer
(**2 seconds**, configurable) rather than queried per sighting — at 104
sightings a minute a per-sighting query puts a network round trip on the hot
path of the pipeline. The refresh interval is a **staleness budget**, not a
cache: the graded clock starts when an operator adds a plate, so the window
before every worker knows about it is inside the 5-second budget.

### 4.2 A match is not a boolean

| Tier | Condition | Operator meaning |
|---|---|---|
| **confirmed** | Exact normalised-key match **and** confidence ≥ 0.85 | Act on it |
| **probable** | Exact key at lower confidence, **or** edit distance 1 | Verify against the evidence crop first |
| **possible** | Edit distance 2 | A lead — corroborate before acting |

**Why not exact matches only.** That misses the wanted vehicle whose plate came
back one character wrong — **measured: 12.7% of reads** (82.4% exact against
95.1% within edit distance 2). A system that misses one wanted vehicle in eight
is not performing its function.

**Why not anything close.** The operator is flooded and stops reading alerts,
which is worse than not alerting.

**Why confidence splits an exact match.** A plate read at 0.4 that lands on a
watchlist entry is more likely to be a misread that coincidentally matched than
a genuine sighting; at 0.95 it is not. The same string is different evidence,
and the tier is how the operator is told which they are looking at.

**Why edit distance survives normalisation.** Positional normalisation (§5.3)
repairs *cross-class* errors — a letter occupying a digit slot. The dominant
residual error is *within-class*: `0` read as `6`, `8` as `B`, inside the same
slot type. Normalisation cannot touch those, because the character is already
the correct class. Edit distance is what catches them.

**Reads too short to identify a vehicle never match at any tier.** Reads under
four characters — which wide-area cameras produce in volume — are barred from
driving an alert: a read of `GJ` is within edit distance 2 of a great many
watchlist entries and within edit distance 0 of none of them, so alerting on it
raises a confident-looking alert from a camera that cannot resolve a plate at
all. The read is still persisted and still searchable; it is only barred from
*causing* an alert.

### 4.3 Watchlist sources

Every source sits behind one provider interface: VAHAN (stolen and blacklisted),
eGujCop / CCTNS (wanted persons and case-linked vehicles), SARTHI, and locally
maintained lists.

> For this submission the interface is real and the data behind it is
> **representative, not live**. Stating that plainly is the correct position; no
> team has been granted live VAHAN access for a hackathon, and implying
> otherwise fails at the first question.

### 4.4 Continuous correlation across cameras

**Journey reconstruction.** Given a plate and a window: query sightings ordered
by time; collapse consecutive sightings on one camera into a **visit** with a
first- and last-seen time; compute road distance between consecutive visits over
an OSM Gujarat extract; derive implied speed; and split the sequence wherever it
exceeds ~120 km/h, lowering confidence rather than discarding the outlier.

The collapse matters more than it sounds: a vehicle held in one camera's view —
at a light, in a toll queue — produces a fresh track every 30 seconds by design,
so rendering sightings literally would draw a vehicle teleporting on the spot
dozens of times. A visit is also the honest description of what the camera saw.

The split matters more still. The most probable explanation for one plate
appearing in two places impossibly fast is **two vehicles wearing the same
plate**, so cloned-plate detection is a by-product of route reconstruction —
but only for a system that refuses to silently drop the anomaly.

Output is GeoJSON: a route `LineString` plus timestamped `Point` sightings
carrying evidence-crop references.

| | Budget | Measured |
|---|---|---|
| Journey query, worst of 5 runs | 2 s | **26 ms** |
| p95 under 78-camera write load | — | **31 ms** |

**Appearance re-identification, and the vehicle id.** Every sighting carries a
`vehicle_uid`. It is joined to an earlier sighting by a readable plate where
there is one, and otherwise by appearance: a learned image vector
(DINOv2-small, 384 numbers, `pgvector`) from the largest view of the vehicle,
accepted only when the nearest match is close, clearly closer than any other
vehicle, reachable in the time at ≤ 120 km/h and of the same kind. An operator
traces it by entering `#<id>`.

**Measured 14 Sep 2026** on five government cameras (2,321 vehicles in 15
minutes): 43 links were checked by eye — 34 were the same object, 7 were wrong,
2 unclear; at the shipped threshold 16 of 17 were right, which is why that
threshold ships. **No link crossed cameras**, and the nearest cross-camera
candidates were all different vehicles, so on these feeds this re-finds a
vehicle on one camera rather than following it between cameras. It is a
corroborating lead to check against the evidence images, never an identity
claim: the link type and distance are stored with every sighting, and the plate
trace never follows an appearance link.

---

## 5. AI analytics

The requirement names *"technologies such as ANPR, Facial Recognition Systems
(FRS), object detection, person and vehicle tracking."* Each is addressed below
with its build status stated, because "such as" makes the list illustrative
while ANPR is separately confirmed as the mandatory capability.

| Capability | Status |
|---|---|
| ANPR — detection, localisation, recognition, voting | **built and measured** |
| Object detection — vehicles and vehicle class | **built and measured** |
| Vehicle tracking — multi-object, stable ids across frames | **built and measured** |
| Vehicle re-identification by appearance (vehicle id) | **built and measured** — same-camera on the government feed; no cross-camera link seen there (§4) |
| Camera tamper and health analytics | **built and measured** |
| Person detection and tracking | **design** — the pipeline stage is shared; no person class is enabled |
| Facial recognition | **position stated (§5.6); ships disabled** |

### 5.1 The ANPR pipeline

```
decode → sample → detect + track vehicle → locate plate INSIDE the vehicle box
       → OCR → reject overlay text → per-track vote → normalise → persist
```

**The reader** is docTR (PARSeq) on the CPU since 14 Sep 2026, with Tesseract as
the fallback; §5.5 has the measurement that chose it.

Two of those stages exist because of what real feeds turned out to be, and both
are worth stating because they are not in the obvious design.

**Plate search is confined to a vehicle box.** Every government feed observed
carries burnt-in text — a timestamp band, a camera label, `REC`, a site name —
much of it high-contrast white-on-dark at approximately plate size and aspect.
Running recognition over the whole frame reads the camera's own clock as a
registration number, confidently, on every camera, forever. Confining the search
to a detected vehicle removes the entire error class; a second overlay filter
catches the remainder.

**Sampling follows the scene.** Decoding every frame of every stream and running
a detector on it does not close arithmetically: at 15 fps, 80,000 cameras is 1.2
million detector invocations per second. It is also largely wasted, since many
cameras watch an empty lane for hours. The pipeline therefore decodes
continuously — cheap, and it keeps the session alive — and *analyses* at a rate
that tracks scene activity, with a floor low enough to notice that something has
changed. **measured: 81.3% of analysis avoided** at acceptance.

### 5.2 Per-track voting

A vehicle is typically read in tens of frames, each slightly differently.
Emitting per frame both duplicates the record and discards the most valuable
thing available — many independent opinions of one plate.

Reads are pooled across the track's lifetime and combined confidence-weighted.
Two votes run: a **whole-string** vote, which is conservative and can only
return a string some frame actually produced; and a **per-character** vote,
which can assemble a plate no single frame read correctly. The stronger result
wins.

A track ends when the vehicle leaves the frame, when it has been in view too
long (cut at 30 seconds, so a stationary vehicle's plate is not withheld until
it drives away), or at a scene discontinuity (§3.4). All three write.

### 5.3 Normalisation is positional

Indian plates have a fixed grammar:

```
GJ      01        AB        1234
2 letters  1–2 digits  0–3 letters  4 digits      (BH series also handled)
```

OCR confuses `0`/`O`, `8`/`B`, `1`/`I`. The naive repair — mapping `O → 0`
globally — is wrong, and the failure is silent: `GJ01OB1234` and `GJ0108 1234`
are different vehicles, and a global map merges their histories onto one key.

Each character's target class is therefore decided by **the slot it occupies**,
and coercion happens only within that slot. Normalisation is applied at **both
write and query time** — on write alone an operator's typo never matches; on
read alone the index is inconsistent with itself.

**The confusion table is derived, never authored.** It is generated from glyph
similarity or from substitutions measured in the platform's own reads, over a
small documented prior. A hand-written table cannot distinguish a considered
omission from an oversight, and gives `O → 0` (near-certain) the same weight as
`J → 1` (a stretch).

> **The gaps in that table are load-bearing.** If every character may become
> every other, then every string of the right length is a structurally valid
> plate and format validation stops carrying information. Adding pairs by hand
> is prohibited for that reason.

**Format-validation failures are kept, not discarded** — emitted at reduced
confidence and flagged. A mis-read wanted vehicle is a worse outcome than a
noisy record.

### 5.4 Measured accuracy, per condition

A single headline figure would average three different problems, so it is not
quoted.

| Condition | Exact | Within edit distance 2 |
|---|---|---|
| Daylight | **86.6%** | 98.1% |
| Glare | **57.1%** | 92.9% |
| Night | **58.6%** | 75.9% |
| **Overall, ANPR-grade cameras** | **82.4%** | **95.1%** |

Night is the weak spot and is reported as such. The mitigation path is §5.5.

### 5.5 Model selection — light by default, heavy only when earned

The cheap path wins on average, and that is a measurement rather than a
preference:

| | Measured |
|---|---|
| Classical plate localisation | **1.7 ms** per call |
| Learned localisation, CPU provider | **326 ms** per call |
| Single-image GPU inference | **24.4 ms** vs classical **17.0 ms** — transfer overhead dominates |
| Tesseract on Indian plates | **83% exact** |
| A general hub OCR model on the same crops | **1.1% exact** — it misreads the Indian font systematically |
| docTR (general OCR, CPU), 14 Sep 2026 | night plates found **62%** vs Tesseract's **35%** on the same clips; **37 ms** vs ~100 ms per crop; 24 vs 5 of 42 real government plates exact. **Now the default reader**, Tesseract the fallback |

That last pair is the most important line in this section: a heavier, more
modern model was **not** better here, because it had never seen this font.

Cameras the light path demonstrably cannot read escalate **on their own
evidence** — one rung at a time, inside a budget, and rolled back automatically
if the upgrade does not measurably help. Escalation is per camera, because the
grid is not uniform and a threshold tuned for one resolution silently skips
readable vehicles on smaller cameras while wasting recognition on unreadable
ones elsewhere at the same time. No threshold in the platform is expressed in
absolute pixels; per-camera stream properties are held in the registry.

**An operator can also ask for the heavy reader.** When an investigation narrows
a vehicle down to a place, `POST /api/ocr-boosts` switches the cameras within a
chosen radius (or a named set) to PaddleOCR-VL on a GPU for 1–720 minutes. On
the generated night clips it found **101 of 101** plates against docTR's 63.
The boost always expires, is capped at 12 cameras, is audited, and each worker
reports back whether it is running or why it could not (for example, no GPU).

### 5.6 Facial recognition — the position

The platform implements FRS as a **pluggable analytics module in the same edge
pipeline** — the same decode, sample, track and vote stages, with a different
recogniser — and **ships it disabled**.

It is designed to be enabled only under a lawful authorisation regime, with:

- **per-query case binding** — every query records the case reference that
  authorises it
- **an immutable audit entry per query** — who, what, when, under what authority
- **no blanket enablement on public feeds**, ever, as a matter of configuration
  rather than of policy documentation

The reason is stated rather than implied: deploying facial recognition against
public CCTV raises live questions under the **DPDP Act 2023**, and India has no
settled regulatory framework for it. A system that can be enabled under
authorisation, and that records why it was, is the defensible design. One that
is silently on by default is not.

> **Demonstration policy.** Where the module is shown, it is shown on
> **synthetic faces only** — generated faces belong to no person, so no consent
> question arises and no biometric data of any real individual is processed. It
> is never pointed at a government feed or at any recording of a real person.

**This section is the deliverable for HLD element 5.** The module's
demonstration is scheduled after every mandatory artifact is complete, because
bonus capability does not compensate for a missing mandatory one.

### 5.7 Analytics beyond ANPR

**Vehicle class and appearance attributes** accompany each sighting and are
carried into the export and the evidence bundle, along with the vehicle id (§4)
and, for a vehicle with no readable plate, the evidence image the appearance
vector came from.

**Camera tamper detection** identifies a covered, moved or defocused camera
against a per-camera reference that the platform calibrates from that camera's
own normal — not against a global threshold, because the grid is not uniform.
**measured: false positives reduced from 39 to 0** after self-calibration
replaced fixed thresholds.

**Health analytics** probe every camera every cycle and decide status from the
**transport result**, never from the upstream's own declared status. This is not
a hypothetical: two cameras in the evaluation grid reported `"status": "live"`
while returning HTTP 500 on every stream request, consistently, over days.
Mirroring the declared field would have shown an operator coverage they did not
have.

---

## 6. Alert generation and notification workflow

### 6.1 The workflow, end to end

```
 sighting written
   │
   ├─ matched against the in-process watchlist          §4.1
   │
   ├─ tier assigned: confirmed │ probable │ possible     §4.2
   │
   ├─ PRIORITY = watchlist severity − tier demotion
   │             clamped to the console's range
   │             (confirmed −0, probable −1, possible −2)
   │
   ├─ DEDUPLICATION: suppressed if the same plate raised
   │   an alert on the same camera within the window
   │     · 120 s default for confirmed and probable
   │     · × 3 for `possible` — a low-tier repeat is far
   │       more likely to be the same vehicle re-read than
   │       a second event worth waking someone for
   │
   ├─ ALERT ROW written in the SAME TRANSACTION as the
   │   sighting — both commit, or neither                §1.5
   │
   ├─ EVIDENCE BUNDLE assembled and carried on the alert
   │     plate read · confidence · matched watchlist record
   │     · category · severity · case_ref · tier · priority
   │     · camera id, name, district · lat/lon
   │     · evidence crop · sighting timestamp
   │     · detection latency, measured per alert
   │
   └─ OPERATOR STATES
         new ──► acknowledged ──► actioned
          │            │
          └────────────┴──► dismissed │ false_positive
```

### 6.2 Prioritisation

Priority is **watchlist severity demoted by match uncertainty**. A high-severity
entry matched at `possible` outranks a low-severity entry matched at
`confirmed` only if its severity exceeds it by more than two — which is the
intended behaviour, because severity is a statement about the vehicle and the
tier is a statement about the read.

### 6.3 Deduplication

A vehicle in view of one camera is re-read repeatedly by design. Without
suppression, one stationary wanted vehicle would produce an alert every 30
seconds until it moved. The window is per plate, per camera, and widened for the
lowest tier.

### 6.4 Operator interaction and visualisation

The alert console presents alerts ordered by priority with unacknowledged counts
surfaced separately, each carrying its evidence bundle and a map pin. From an
alert an operator can open the camera live, open the vehicle's reconstructed
journey, or export the record. From a trace, one button boosts the cameras
around the vehicle's last sighting to the heavy reader (§5.5).

> **There is no delete.** `dismissed` and `false_positive` are *statuses*, not
> removals. Every alert, its status, who set it, when, and any resolution note
> remain in the record. An alert an operator judged wrong is itself evidence —
> of the platform's error rate, and of the decision taken.

### 6.5 Notification routing

**built:** every alert carries the camera's district and jurisdiction is
filterable in the console and the API, so a district's operators can work only
their own alerts.

**design:** *push* routing — dispatching an alert to a jurisdiction's console,
SMS or radio channel, and escalating it when it remains unacknowledged past a
threshold. The data required is present on every alert row; the delivery
integrations are not built, and are named in §8 as a rollout prerequisite.

### 6.6 Measured latency

| | Budget | Measured |
|---|---|---|
| Detection to alert on screen | 5 s | **1.04 s** |

Reported per alert, not once at acceptance: `detection_latency_s` is computed on
every alert from its own timestamps, so the figure on the performance page is
the live distribution rather than a rehearsal.

---

## 7. Scalability, interoperability, security and performance at 80,000 cameras

### 7.1 Sizing the analytics tier

**measured**, on a 20-core / 125 GB host:

> **50–57 cameras per box.** At 57 cameras OCR p50 was **624 ms** with a healthy
> **6.1%** shed. At 76 cameras the same box shed **94.7%** of OCR attempts and
> OCR p50 rose to **3.4 s**.

Shedding is designed behaviour — a thread blocked in recognition has stopped
decoding, and a starved decoder loses whole vehicles rather than one vehicle's
vote — but above ~57 cameras it is a capacity limit rather than a free lunch.

**Raising concurrency makes it worse.** measured at 77 cameras: moving OCR
concurrency from 2 to 4 took the shed rate from **84.5% to 96.9%** and OCR p50
from **1.7 s to 15.7 s**. Widening a bound on a saturated resource does not
create CPU.

At **50 cameras per node**:

| | |
|---|---|
| Nodes for 80,000 cameras | **arithmetic: 1,600** |
| Distributed across 33 districts | ≈ **48 per district** |
| Node specification | 20 cores, 32 GB (**measured** use: 8.1 cores, ~12 GB across 6 shards) |
| Placement | District or corridor, beside the cameras they read |
| Scaling operation | Increase the replica count |

> **1,600 nodes is arithmetic, not a distributed-systems project.** Workers claim
> a shard with a database advisory lock, so a replica computes its own share of
> the estate with **no orchestrator, no leader election and no per-replica
> configuration** — and a crashed worker returns its shard the instant its
> connection drops, with no timeout, heartbeat or reaper.

That property was earned rather than designed in. An earlier version sharded by
container hostname, which Compose sets to a random hex id; six replicas took
shards 0, 0, 3, 3, 3, 4 — a third of the estate processed three times over while
two shards were never processed at all, with nothing reporting an error.

**What would change the 1,600 figure**

- **A GPU with cross-camera batching.** Single-image GPU inference measured
  *slower* than the CPU path here (24.4 ms vs 17.0 ms) because transfer overhead
  dominates. Batching across cameras is an architecture change, not a
  configuration flag, and it is the single largest available win — plausibly
  3–5× per node, taking 1,600 nodes toward ~400.
- **An India-trained plate recogniser.** See §5.5. Higher accuracy and, being a
  loadable model rather than a subprocess per crop, plausibly lower cost too.

Neither is claimed. Both are named because a sizing model that cannot say what
would move it is not a model.

### 7.2 Sizing the index

**measured: 104 sightings/minute** across 71 contributing cameras — about **1.5
sightings per camera per minute** on a moderately busy road.

| | Arithmetic | Result |
|---|---|---|
| Sightings/day at 80,000 cameras | 80,000 × 1.5 × 1,440 | **~173 million/day** |
| Row size | measured | **~300 bytes** |
| Raw storage/day | 173 M × 300 B | **~52 GB/day** |
| After columnar compression (7-day policy, segmented by camera) | ~10× typical | **~5 GB/day** |
| Evidence crops | ~8 kB × 173 M | **~1.4 TB/day** |

**The crops dominate by roughly 25×**, so their retention is separate from the
rows'. Crops are pruned by day, and a trace still works after its crops have
aged out — the record states that the crop is gone rather than pretending there
never was one.

**The rows are cheap enough to keep for years**, which matters precisely because
the value of this index is that it already contains the answer to a question
nobody has asked yet (§0).

| Tier | Content | Volume |
|---|---|---|
| Video | Remains on departmental NVR/VMS | **0 added centrally** |
| Hot | 7 days of events, uncompressed | ~365 GB |
| Warm | 90 days, columnar | ~450 GB |
| Crops | 7 days | ~10 TB — one storage server, not a programme |

Against a central-VMS design's **~26 PB** at 15-day retention.

### 7.3 Network at scale

| Path | Load |
|---|---|
| Metadata, steady state | 80,000 × ~0.5 kbit/s ≈ **40 Mbit/s** statewide *(from the measured per-camera rate)* |
| On-demand video at 1% concurrent viewing | 800 × ~3 Mbit/s ≈ **2.4 Gbit/s** |
| **Peak total** | **< 2.5 Gbit/s**, against **240 Gbit/s** for central ingest |

### 7.4 Query performance at scale

The journey query is an indexed equality on the normalised plate plus a time
range, with time-partition exclusion.

| | Budget | Measured |
|---|---|---|
| Journey query | 2 s | **26 ms** worst of 5 |
| p95 under 78-camera write load | — | **31 ms** |
| Camera onboarding | 30 s | **5.1 s** |
| Detection to alert | 5 s | **1.04 s** |
| Concurrent streams held | 50 | **50 for 15 minutes, 0 restarts** |
| Model warm-up per replica | — | **0.3 s** (was minutes when loaded lazily) |

At 80,000 cameras the index is larger but **the query is unchanged in shape** —
it selects a few hundred rows by indexed equality within a time window, and
partition exclusion means it never reads outside that window. Trigram indexing
covers fuzzy search; a dedicated search cluster is the scale path if trigram
search on a multi-billion-row table stops meeting the budget, and it is a swap
of one endpoint rather than a redesign.

### 7.5 Interoperability

| Seam | Interface |
|---|---|
| Camera ingress | ONVIF, RTSP, HLS, WHEP/WebRTC, HTTP |
| Platform API | OpenAPI, documented, browsable on the running instance |
| Geographic responses | **GeoJSON**, never ad-hoc coordinate objects |
| Event transport | Kafka wire protocol |
| Watchlist ingress | One provider interface per source system |
| Road network | OSM extract via a standard routing engine |
| Export | CSV and PDF |

There is no proprietary interface between the platform's own layers (P5). The
practical test of this claim: the live API documentation page is itself a
submission artifact, and every endpoint carries a summary.

### 7.6 Security

| Control | Status |
|---|---|
| Credentials resolved from the environment, never stored in the registry; embedded-credential URLs rejected with 422 | **built** |
| Authentication, with role- and department-scoped authorisation derived from registry ownership | **built** |
| Immutable append-only audit of every stream view, plate search, journey query and export | **built** |
| Purpose binding — a case reference recorded against a query | **built** |
| Alerts never deleted; dismissal is a status | **built** |
| Managed secret vault with rotation | **design** |
| mTLS between services | **design** |
| Network segmentation isolating the camera VLAN from analytics and presentation planes | **design** |

**Authorisation is data-driven, not code-driven.** Scope comes from the
registry's ownership columns, so a Food & Civil Supplies operator sees godown
cameras and SCRB sees the estate, without a code path per department.

**The audit write must never fail the operation it describes.** An audit insert
that raises would turn a working plate search into an error, which is a worse
outcome than a gap in the log; failures are logged loudly instead.

### 7.7 Privacy — DPDP Act 2023

**Metadata-by-default is itself the primary privacy control.** The system does
not centralise footage it has no need for: video remains on departmental
infrastructure and is pulled only when a person opens it, which means the
default state of the platform is that no personal video has moved anywhere.

On top of that: purpose-bound access with a case reference on every query;
tiered retention with automated expiry, crops expiring well before rows; an
immutable record of who accessed what and under what authority; and facial
recognition disabled by default with the position stated in §5.6.

### 7.8 Availability and failure

Each row has been **observed on this build**, not imagined.

| Failure | Behaviour |
|---|---|
| An ANPR node dies | Its advisory locks drop; another replica reclaims the shard on its next start. Cameras on that node stop being read until then — a recall gap, not an outage |
| An upstream feed flaps | The relay restarts it, counts restarts, and the camera is reported **`unstable`** — a third state beside up and down, so an operator is not told the platform is broken when the source is |
| A camera cannot be decoded directly | Analytics fall back to the relayed stream. **measured: recovered 10 of 31** government feeds that could not be opened over progressive HTTPS |
| The router is unavailable | Journeys fall back to great-circle distance per segment, flagged. A router that cannot route one pair degrades that hop, never the response |
| The database drops a connection | The sink retries once on a fresh connection. **measured: 4 of 484 reads** were being lost to this before the retry existed |
| A write is refused for any other reason | Counted per camera and surfaced as write health, which reports what was *produced* against what was *persisted* |
| Site or state outage | Active-passive DR between the SDC and a secondary site — **design** |

That last built row exists because of the worst defect in this build: a
parameter-order error failed **every write in the estate for two hours** while
the throughput counters climbed normally.

> The lesson generalises, and it is why every figure in this document comes from
> the index rather than from the pipeline's own counters: **a counter
> incremented when work is created proves nothing about work being completed.**

### 7.9 Stack deviations, and why

Justified deviation is defensible; unexplained deviation is not.

| Suggested | Chosen | Justification |
|---|---|---|
| Leaflet | **MapLibre GL** | DOM markers do not survive statewide camera density; the claim is 80,000 points, and a map that cannot draw them contradicts it |
| Elasticsearch | **TimescaleDB + trigram** | One database rather than two. Registry, geospatial and time-series in one store removes a distributed-consistency problem we do not need. A search cluster is the scale path, not the starting point |
| Kafka | **Redpanda** | Kafka wire-compatible, single binary, no ZooKeeper — same API, far lower operational surface |
| *(not suggested)* | **pgvector** | Vehicle appearance search (DINOv2-small, 384 numbers) for plate-unreadable correlation, in the database already present |
| *(not suggested)* | **MediaMTX** | A concrete implementation of the WebRTC/HLS relay the brief already anticipates |

---

## 8. Prerequisites, assumptions and what departments must supply

### 8.1 Assumptions

| # | Assumption | If it does not hold |
|---|---|---|
| A1 | Feeds are reachable from the node that analyses them, directly or via a departmental gateway | Analytics move behind the departmental boundary; the metadata contract is unchanged |
| A2 | Read-only service credentials can be issued per department | Onboarding is limited to feeds already public-facing |
| A3 | Cameras carry, or can be surveyed for, a position | Positions are derived by place name at stated precision (§2.3), and coverage analysis is scoped accordingly |
| A4 | Plate crops reach roughly 80 px in width at the point of capture | The camera is graded non-ANPR and reported as situational-awareness only (§9) |
| A5 | District sites can host a 20-core class node | Cameras backhaul to the next tier; the per-node figure is unchanged, the count of sites is not |
| A6 | Watchlist source systems can expose a query or a periodic extract | The provider interface is fed by manual list maintenance |

### 8.2 Prerequisites for rollout

Named plainly, including the ones this build does not satisfy:

- **Edge autonomy** — the on-disk store-and-forward buffer and local watchlist
  cache described in §1.5 and §3.5. **Not built.**
- **Notification delivery** — push routing to a jurisdiction's console, SMS or
  radio, and escalation on unacknowledged alerts (§6.5). **Not built.**
- **Secret vault, mTLS, VLAN segmentation** (§7.6). **Not built.**
- **ONVIF, vendor-SDK and VMS-API adapters** — shaped by the built interface,
  untested for want of a device or instance to test against (§2.1).
- **Live watchlist integration** to VAHAN / eGujCop / SARTHI, replacing the
  representative dataset behind the provider interface (§4.3).
- **An India-trained plate recogniser**, which is the largest single accuracy
  gain available (§5.5) and the one that addresses the night figure.

### 8.3 Information required from participating departments

To assess integration feasibility, each department must supply:

- **Camera inventory** — make, model, firmware, codec, resolution, frame rate
- **Network reachability** — IP addressing, NAT and firewall posture, available
  bandwidth per site
- **VMS/NVR in use**, its version, and whether an API or SDK licence is held
- **Credentials policy** — whether read-only service accounts can be issued
- **Current retention period** and where recordings are stored
- **AMC status and expiry** — this determines what may be modified against what
  must be worked around, and is the single most common cause of an integration
  that is technically possible and contractually blocked
- **Precise geolocation, mounting height, bearing and field of view** — these
  are not decoration; they drive coverage polygons and route plausibility, and
  a camera without them is placed at stated uncertainty (§2.3)
- **Data-sharing authority** — which feeds may be centrally viewed, by whom,
  under what legal basis

### 8.4 What the platform requires operationally

| | |
|---|---|
| Compute | One 20-core / 32 GB class node per ~50 cameras, sited by district or corridor |
| Central | One database host for registry, index and geospatial data; API and console tier; a routing engine with an OSM extract |
| Network | ~0.5 kbit/s per camera sustained northbound; on-demand video sized to expected concurrent viewing, not to camera count |
| Storage | ~5 GB/day compressed rows at 80,000 cameras, plus crop retention chosen per policy |
| Operations | No orchestrator, no leader election, no per-replica configuration (§7.1) |

---

## 9. What this platform does not claim

A design document that lists only strengths cannot be checked. These are the
limits, stated in the same document as the claims.

- **All 30 government cameras in the evaluation estate fall below ANPR grade,
  and none of the 50 simulated ones do** — the platform grades this itself, from
  crops it measured, and says which. Their crops average **66 px** across —
  about 7 px per character — against a purpose-built feed's 276 px. They are
  correctly deployed situational-awareness views and are graded as such. **Every
  accuracy figure in this document is scoped to ANPR-grade cameras; there is no
  estate-wide number**, because one would be arithmetic over two different
  populations.
- **Accuracy is reported per condition** (§5.4). A single headline would average
  three different problems, and night is materially worse than daylight.
- **Aggregate ingest measured on the test estate is a property of the test
  content**, which is synthetic and compresses far better than real CCTV. It is
  not a bandwidth capability figure and is not used in §7.
- **The 1,600-node figure assumes traffic like the measured corridor's.** A
  camera on a quiet road costs less; a toll plaza costs more. Size on measured
  sightings-per-camera-per-minute at the actual site — a figure this platform
  reports per camera, which is what makes the model checkable at rollout rather
  than only at design time.
- **Edge autonomy, push notification, mTLS, the secret vault and the VMS
  adapters are design, not build** (§8.2). They are specified here because the
  architecture is incomplete without them, and labelled because a proposal that
  blurs the two cannot be trusted on the parts that *are* built.
- **Facial recognition ships disabled** and is demonstrated only on synthetic
  faces (§5.6).
- **Watchlist data is representative, not live** (§4.3).

---

## Appendix — where the numbers come from

| Source | Contains |
|---|---|
| `docs/model-selection.md` | The architecture-choice justification, and the arithmetic rejecting central ingest |
| `docs/anpr-method.md` | The recognition pipeline in detail |
| `README.md` | The system as built, its design rules and its measured figures |
| `scripts/acceptance/` | The per-milestone acceptance tests that produced the measured figures |
