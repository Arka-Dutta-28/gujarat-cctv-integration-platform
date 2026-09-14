# Model Selection Analysis & Justification
## Gujarat Police Hackathon 2026 — Integrated Video Management & Analytics Platform

> Maps directly to Solution Presentation requirement #1: *"Proposed solution model (Reference Model 1–5, Hybrid, or Customised Architecture) **with justification**."*

---

## 1. What we are actually optimising for

The evaluation is qualitative across seven areas, but they are not equally winnable. Ranked by leverage:

| Criterion | Leverage | Why |
|---|---|---|
| 01 Successful Test Case | **Decisive** | Binary-ish. If ~50 feeds don't onboard and the vehicle isn't traced, nothing else rescues you. |
| 04 Working Platform & Demo | **Decisive** | Explicitly disqualifies mock-ups. Working software is a gate, not a score. |
| 06 Scalability & PoC Readiness | **High** | Where most teams will hand-wave. Concrete numbers separate you. |
| 05 Video Analytics Output | **High** | ANPR quality is measurable and comparable across teams. |
| 03 Solution Architecture | Medium | Judged on soundness and clarity, not novelty. |
| 02 Solution Presentation | Medium | Table stakes, easy to do well. |
| 07 Submission Completeness | Medium | Pure discipline. Free marks; don't drop them. |

**Implication:** the model choice must be the one that *maximises probability of a working test-case demo* while *still carrying a credible 80,000-camera story*. Those two pull in opposite directions. That tension is the whole decision.

---

## 2. Scoring the candidate combinations

Model 1 is mandatory and must be combined with at least one other. Five realistic combinations:

| Combination | Test-case winnability | 80k scale story | Cost story | Build risk in hackathon window |
|---|---|---|---|---|
| **M1 + M2** | **High** — direct RTSP/ONVIF connect is exactly the shape of what the Resources page will hand you | **Weak** — 80,000 direct centre-to-camera sessions is not a real architecture | Good | **Low** |
| **M1 + M3** | **Low** — there is nothing to federate. You will not be given live Milestone/Genetec/Hikvision CMS instances with API credentials to demo adapters against | **Strong** | Good | **High** — the core claim is undemonstrable |
| **M1 + M4** | Medium — centralising 50 feeds is fine | **Catastrophic** — see §3 | **Terrible** | **Very high** |
| **M1 + M2 + M3** *(edge-first hybrid)* | **High** | **Strong** | **Strong** | **Medium** |
| Fully custom | Varies | Varies | Varies | Highest — you forfeit the "we chose a reference model and justified it" framing for no gain |

---

## 3. Why Model 4 (Central VMS) is a trap

Run the arithmetic the problem statement invites you to run, at its own stated target of ~80,000 cameras:

| Quantity | Conservative (2 Mbps / 720p) | Realistic (4 Mbps / 1080p) |
|---|---|---|
| Sustained backhaul to centre | **160 Gbps** | **320 Gbps** |
| Raw video per day | 1.73 PB | 3.46 PB |
| Storage at 15-day retention | **~26 PB** | **~52 PB** |

Two things follow:

1. **It contradicts the stated Core Goal.** The problem statement asks for an approach that is *"cost-effective"* and *"uses existing infrastructure to the maximum practical extent."* Central VMS discards existing departmental VMS, storage and AMC investments across 26 departments and rebuilds them centrally. It is the least compliant option with the goal as written.
2. **It contradicts the bonus criteria.** Bonus consideration explicitly rewards *"strong edge-processing, bandwidth-optimisation, or low-connectivity operation."* Central ingest is the opposite of all three.

**Do not propose Model 4 as your primary model.** Do, however, *cost it out in your presentation* — showing the 26 PB / 320 Gbps figures and explaining why you rejected them is one of the strongest slides you can put in front of a technical evaluator. It converts a rejected option into evidence of engineering judgement.

---

## 4. Why neither M2 nor M3 works alone

**Model 2 alone** is the fastest path to a working demo and the worst path to a scalability score. The problem statement pins it down: the platform *"connects directly to each departmental CCTV or VMS system"* and integrates streams *"without introducing an intermediate middleware or federation layer."* That is fine for 50 cameras and structurally indefensible for 80,000 — you would be arguing for 80,000 long-lived direct sessions terminating on a central platform, with no abstraction between the centre and every vendor quirk in the State.

**Model 3 alone** is the correct scaling abstraction and undemonstrable in this hackathon. Its deliverable is *"working middleware demo federating at least two different systems"* — but the ~50 test cameras will almost certainly arrive as raw stream endpoints, not as departmental VMS instances you can federate. You would be presenting an adapter framework with nothing real plugged into it.

---

## 5. Recommendation

> **Propose a Hybrid Architecture: Model 1 as the control plane, Model 2 as the demonstrated feed-access path, Model 3 as the structural spine, delivering Model 4's analytics outcomes without Model 4's central-ingest cost.**

### The single organising idea

**Move the analytics to the edge. Move only metadata to the centre. Move video only on demand.**

Video stays where it already lives — on departmental VMS and NVRs, untouched. What crosses the WAN by default is an ANPR event of roughly 250 bytes, not a 2 Mbps stream. Full video is pulled centrally only when an operator clicks a camera or an alert needs corroboration.

| | Central VMS (M4) | Edge-first hybrid |
|---|---|---|
| Steady-state WAN | ~160–320 Gbps | **~160 Mbps** metadata + ~1.6 Gbps on-demand video at 1% concurrency |
| Central storage @ 15 days | ~26 PB | **~100 TB** (metadata + hit thumbnails) |
| Existing departmental infra | Replaced | **Preserved and reused** |
| Behaviour on WAN outage | Blind | **Edge keeps detecting; store-and-forward on reconnect** |

That is a **~1,000× reduction in sustained bandwidth** and roughly **250× in central storage**, at equal or better analytics latency — because detection happens metres from the camera rather than after a 1,000 km round trip.

### How each model earns its place

| Model | Role in the hybrid | Demonstrated how |
|---|---|---|
| **M1 — Registry & GIS** *(mandatory)* | **The control plane, not a side deliverable.** Every other layer reads from it: which cameras exist, where they are, which adapter to use, what their FOV and bearing are, whether they are healthy. Route reconstruction is impossible without its geospatial data. | Registry portal, bulk + manual + API onboarding of the ~50 test cameras, GIS map, health monitoring, gap analysis |
| **M2 — Unified viewing & metadata analytics** | The **feed-access and operator-experience layer**. Direct RTSP/ONVIF/SDK access is genuinely the right answer for cameras with no VMS in front of them — which is most of them. | Unified control-room view, video wall, ANPR metadata, searchable sightings |
| **M3 — Federation & adapters** | The **structural spine that makes M2 survive scale.** Every feed source goes through a normalising adapter, so a new vendor is a plugin, not a redesign. Departmental VMS instances federate through the *same* interface as bare cameras. | Adapter framework demoed against ≥2 genuinely different source types (e.g. bare RTSP + an ONVIF device or a VMS API) |
| **M4 — Central analytics outcomes** | Its *goals* — statewide vehicle tracking, route reconstruction, watchlist integration — are adopted in full. Its *centralised ingest and storage* are rejected on cost and Core-Goal grounds. | Cross-camera journey reconstruction, watchlist correlation, real-time alerts |

---

## 6. The honest counterargument

You should be ready for this in Q&A, because a sharp evaluator will raise it.

**"Model 4 is the one that actually lists what you're asking us to build — statewide vehicle tracking, VAHAN/SARTHI/eGujCop/AFIS/NAFIS integration. Isn't hybrid just dodging the hard version?"**

The response has three parts:

1. **We adopt every functional outcome in Model 4's feature list.** Statewide vehicle tracking, route reconstruction, watchlist integration, ANPR, RBAC, DR — all present. What we decline is one specific implementation choice: hauling raw video to the centre. The capability is identical; the transport is not.
2. **The Core Goal is a cost and reuse constraint, and Model 4 violates both.** The brief asks for cost-effectiveness and maximum reuse of existing infrastructure. Model 4 replaces 26 departments' storage and VMS investments. Our approach leaves them running.
3. **The bonus criteria signpost this path explicitly** — "innovative hybrid or customised architecture with clear operational value" and "strong edge-processing, bandwidth-optimisation, or low-connectivity operation" are two of the six named bonus areas. Hybrid is not a hedge; it is the direction the brief points.

**Second counterargument to prepare for: "You are proposing edge hardware you cannot deploy for the demo."**

True, and it must be handled honestly rather than glossed. For the ~50 test cameras you will have no hardware at their sites, so demo analytics run on your own server pulling those streams. The mitigation — and it is a genuinely strong one — is to make the edge analytics unit a **single container image that runs unchanged** on a Jetson Orin, a district GPU server, or your demo box, and then **actually demonstrate it in both placements**: one instance processing test feeds centrally, a second instance on a separate physical machine publishing to the same event bus. That is a real PoC-readiness demonstration rather than a claim.

---

## 7. What this buys you against the bonus list

| Bonus criterion | Covered by |
|---|---|
| Innovative hybrid or customised architecture with clear operational value | The edge-first hybrid itself, backed by the bandwidth/storage arithmetic |
| Advanced cross-camera vehicle movement tracking or multi-camera correlation | Journey reconstruction with travel-time plausibility scoring; vehicle re-ID embeddings for plate-unreadable stitching |
| Additional reliable analytics beyond mandatory ANPR | Vehicle class/colour attributes, ByteTrack multi-object tracking, camera-tamper and health detection |
| Strong edge-processing, bandwidth-optimisation, low-connectivity operation | Core to the design — local watchlist cache, store-and-forward, adaptive frame sampling |
| Enhanced cybersecurity, privacy, auditability, RBAC | Metadata-by-default minimises exposure; per-department RBAC from the registry; immutable audit trail; DPDP-aligned retention |
| Operational dashboards, automated alerts, health monitoring, integration-ready APIs | Registry health monitoring, alert console, documented OpenAPI surface |

Six for six — but only if each is *demonstrated working*, since the brief is explicit that bonus features do not compensate for a missed mandatory requirement.

---

## 8. Decision summary — one paragraph for the deck

> We propose a **Hybrid Architecture** built on the mandatory Model 1 registry as its control plane, combining Model 2's direct feed access with Model 3's adapter-based federation spine, and delivering Model 4's analytics outcomes through distributed edge inference rather than centralised video ingest. Raw video remains on existing departmental infrastructure; only ANPR metadata traverses the state network, with full video pulled on demand. At the target scale of 80,000 cameras this reduces sustained backhaul from an estimated 160–320 Gbps to under 200 Mbps and central storage from ~26 PB to ~100 TB, while preserving every functional capability the challenge requires and leaving 26 departments' existing VMS, storage and AMC investments intact.

---

## 9. What the built system actually measured

Written after the build. Every estimate above that has since been measured is
replaced here. Where a measurement contradicts an estimate, **the measurement
wins and the estimate stays visible** so the change is auditable.

### The bandwidth claim, measured rather than assumed

| | Estimated in §5 | Measured on the 81-camera estate |
|---|---|---|
| Metadata rate per camera | ~2 kbps | **104 sightings/min across 71 contributing cameras**, a few hundred bytes each ≈ **0.5 kbit/s per camera** |
| Statewide metadata at 80,000 | ~160 Mbps | **~40 Mbit/s** — four orders of magnitude below central ingest, better than claimed |
| On-demand video | asserted | **0.23 s** from operator click to first frame; relay process reaped 45 s after the last reader disconnects |

The estimate was conservative. The argument survives its own measurement, which
is the only version of it worth putting in front of an evaluator.

### The compute claim, corrected downward

§5 of the HLD assumed ~60 streams per L4 GPU and derived ~1,340 GPUs statewide.
**That assumption was never realised and must not be quoted.** Two measurements
killed it:

- `onnxruntime-gpu` could not be installed on this link (PyPI at ~132 kB/s, a
  280 MB wheel, repeated zero-byte stalls). The ONNX models run **CPU-only**.
- Where a GPU path did exist, it was *slower*: YOLO11n on `cuda:0` measured
  **24.4 ms** per frame against the classical motion path's **17.0 ms**,
  because inference is unbatched. One stream per process does not fill a GPU.

The honest, measured sizing unit is therefore a CPU box, not a GPU:

> **One 20-core box comfortably carries ~50–57 cameras of this pipeline.**
> At 76 cameras, 94.7% of OCR attempts are shed and OCR p50 degrades to 3.4 s.
> At ~57 cameras, shed is a healthy 6.1% and OCR p50 is 624 ms.

At 80,000 cameras that is **~1,450 twenty-core nodes ≈ 44 per district**. It is
a larger number than the GPU estimate and it is the one that is defensible.
Batched GPU inference is the obvious next optimisation and is stated as
*unrealised*, not as a result.

**This does not weaken the model choice — it strengthens it.** The whole reason
edge-first wins is that analytics cost scales with camera count and must be
distributed; a measurement showing the per-node ceiling is exactly the evidence
that centralising was never viable.

### The accuracy claim, reported per condition

Never quote a single averaged accuracy figure. Measured against ground truth
under full load, 670 sightings:

| Condition | Exact | Within edit distance 2 (the `probable` tier) |
|---|---|---|
| Day | **86.6%** | 98.1% |
| Glare | 57.1% | 92.9% |
| Night | 58.6% | 75.9% |
| **Overall** | **82.4%** | **95.1%** |

The gap between the two columns is the entire argument for match tiers rather
than booleans: at night, exact matching finds 58.6% of vehicles and the
`probable` tier finds 75.9%.

### The finding that most affects the submission

> **No government camera in the old estate reached ANPR grade.** Plate crops
> measured **66 px wide against the simulated estate's 276 px**; 1.7 characters
> read per crop against 9.4; 15% of crops ≥80 px against 94%.

This is a property of the feeds, not of the pipeline, and it is the single
biggest risk to Deliverable 4 and evaluation criterion 05. Two things now
address it, and both are untested against the new grid:

1. The new Sentinel gateway offers **direct RTSP at source**, where the old
   estate was consumed as web-transcoded progressive HTTPS. If the downscale
   was the transcode's doing, the crop problem may simply not exist any more.
   **This is the highest-value unknown in the project**.
2. Per-camera measured escalation to heavy models
   (`services/anpr/escalation.py`) exists precisely for cameras the light path
   cannot read, and rolls back if the heavy path does not demonstrably help.

If both fail, the mitigation stated in the submission plan holds and should be
said out loud: the **output report carries the evidence**, and the platform
reports honestly which cameras are ANPR-grade
(`/api/cameras/anpr-capability`) rather than claiming reads it cannot make.

### Bonus criteria — status against §7, as built

| Bonus criterion | Claimed in §7 | Built and measured? |
|---|---|---|
| Innovative hybrid architecture with operational value | The edge-first hybrid | **Yes** — measured bandwidth and per-node ceiling above |
| Cross-camera tracking / multi-camera correlation | Journey + re-ID | **Yes** — journey p95 **31 ms**; re-ID separation **0.5677** (same plate 0.0186 vs different 0.5863), 10 plate-unreadable pairs stitched in 6 h |
| Reliable analytics beyond mandatory ANPR | Vehicle attributes, tracking, tamper | **Yes** — tamper detection self-calibrating, false positives **39 → 0** |
| Edge processing, bandwidth optimisation, low connectivity | Core to the design | **Partly** — bandwidth and adaptive sampling measured (**81.3%** of frames never reach a detector); the dual-placement edge demo is **not yet done** (M14) |
| Cybersecurity, privacy, auditability, RBAC | Metadata-first, RBAC, audit | **Yes** — auth, audit router and RBAC built in M8 |
| Dashboards, automated alerts, health monitoring, integration-ready APIs | Console + OpenAPI | **Yes** — alert p95 **1.08 s**, health sweep of 80 cameras in **5.2 s**, live Swagger |

Five of six demonstrated and measured; one partial. The gap is the edge
dual-placement demo, which is scheduled and cheap.
