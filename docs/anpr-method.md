# ANPR: what the pipeline does, and what the numbers mean

Written alongside the M3 build rather than after it, because the build plan is
explicit that instrumentation retrofitted later is wasted work — and because
the figures here are a graded submission artifact, not debugging output.

---

## The pipeline

```
decode → sample → detect + track vehicles → locate plate inside the vehicle box
       → OCR → per-track confidence-weighted vote → persist every read
```

Each stage and why it is where it is:

**Decode.** Continuous, per camera, through the adapter the registry names. The
connection is kept live even when nothing is being analysed, because
reconnecting costs more than decoding.

**Sample.** `services/anpr/sampling.py`. An idle camera is analysed at 0.5 Hz;
motion pulls it to 6 Hz *on the frame the motion appears*, not at the next
scheduled tick, and it decays back over three seconds rather than dropping the
moment motion stops — a vehicle briefly behind a pole is still a vehicle.

This is the stage the scalability argument rests on. At 15 fps, 80,000 cameras
is 1.2 million detector invocations a second, which does not close. The field
observations are also explicit that many cameras in the real estate look at a
market stall or an empty lane and will not see a vehicle for hours. The measured
saving is reported by `/api/performance` as `analysed_fraction`.

**Detect and track.** Two interchangeable backends, and which one produced a
number is always reported with the number:

| | vehicles | plates | characters | needs |
|---|---|---|---|---|
| **learned** | YOLO + ByteTrack | YOLO plate detector | plate-recognition net | GPU or a patient CPU, ~9 GB image |
| **lean** | MOG2 background subtraction + IoU tracker | adaptive threshold + contour filter | docTR on CPU (Tesseract fallback) | OpenCV, CPU PyTorch, ~2.6 GB image |

ByteTrack specifically, rather than a simpler IoU tracker, because it keeps
low-confidence detections as association candidates instead of discarding them.
That is exactly the night-with-headlight-glare case, where an IoU tracker drops
the track and one car becomes several sightings seconds apart.

The lean path is not only a fallback. The architecture claims analytics run next
to the camera and only metadata crosses the network, and that claim is much
stronger when the analytics stack fits on a box that can be mounted in a
junction cabinet. On a *fixed* camera — most of an estate — the moving region
genuinely is the vehicle.

`CompositeVehicleTracker` runs the learned detector and switches to motion
proposals on a camera where it has returned nothing across a run of analysed
frames that contained motion. That is a claim about the detector, not about the
road. The switch is counted in the worker's metrics, because a fallback nobody
can see becomes an accuracy figure nobody can explain.

### Which backend actually runs, and why it is the lean one

The table above is written as though the learned path is the better one held
back only by its hardware bill. Measured on this estate, that turned out to be
false at every stage. Each row is an A/B over the same clip or feed, varying one
component and holding the rest fixed:

| stage | learned | lean | outcome |
|---|---|---|---|
| characters | `cct-s-v1-global-model`: **1.1% exact** | Tesseract: **83% exact** | lean, decisively |
| characters, 14 Sep | docTR (CPU, 37 ms): **63/101** night plates found | Tesseract (~100 ms): **35/101** | docTR, now the default |
| localise | ONNX detector: 13/14 read, **55.45 ms** | classical: 13/14 read, **0.93 ms** | lean, 60x cheaper for the same result |
| vehicles | YOLO11n on `cuda:0`: 27 tracked, **24.4 ms** | MOG2: 39 tracked, **17.0 ms** | lean, and the GPU throttled decode 5x |

The recognition result is the least surprising and the most important. The
available hub models are trained on European and Latin American plates, and they
misread the Indian font structurally — `GJ` comes back as `2J`, `3J`, `6J`.
Positional normalisation exists to repair a character in the wrong *class*, so
it cannot help: a `2` in a letter slot gets coerced to a letter, but the model
never saw a `G` to begin with. An India-trained recogniser would very likely
win, and `make anpr-accuracy` is how that would be settled rather than assumed.

The localisation result is the least surprising in hindsight. An Indian plate is
dark characters on a white or yellow ground with a strong aspect ratio, which is
precisely what an adaptive threshold and a contour filter are good at.

The GPU result is the one worth carrying into the sizing narrative. Single-image
inference on a GPU is dominated by host-to-device transfer, so per-call latency
was *worse* than CPU motion detection. A GPU pays for this workload only with
**cross-camera batching** — collecting frames from many cameras into one
inference call. That is an architecture change, not a configuration flag, and it
is the right next step if detector quality ever becomes the binding constraint.

Both choices are environment variables (`ANPR_OCR_BACKEND`,
`ANPR_PLATE_BACKEND`, `ANPR_VEHICLE_BACKEND`) rather than edits, so an evaluator
can re-run any of these comparisons rather than take the table on trust.

### The pipeline was never compute-bound; it was thread-bound

The single largest performance finding of this milestone was not an algorithm.
Every library underneath the pipeline — OpenCV, ONNX Runtime — sizes its
internal thread pool from the machine's core count, and applies it to each
individual operation. This worker already takes its parallelism from one decode
thread per camera. With roughly 81 camera threads across six processes on a
20-core box, the default asked for up to 1,620-way parallelism.

The symptom was that measured stage latencies bore no relation to the work.
`vehicle_detect` reported p50 81.7 ms under load against 8.7 ms probed alone,
and the ANPR tier consumed 19 of 20 cores to perform roughly three cores of
arithmetic. The remainder was threads queueing for one another.

Telling both libraries to work serially — `cv2.setNumThreads(1)`, ONNX
`intra_op_num_threads=1` — moved every number at once:

| | before | after |
|---|---|---|
| ANPR tier CPU | 19.6 cores | **8.1 cores** |
| `ocr` p50 | 16,400 ms | **624 ms** |
| `vehicle_detect` p50 | 81.7 ms | **14.6 ms** |
| `plate_detect` p50 | 167 ms | **2.5 ms** |
| plate accuracy | 10.2% exact | **75.3% exact** |

The accuracy row is the one to dwell on, because it was not an obvious
consequence. Contention made OCR calls slow enough that the bounded OCR stage
began shedding reads, and a track that is read twice instead of eight times
votes from a much weaker sample. The configuration line that halved the CPU bill
also restored the read quality, and neither effect was visible from reading the
code.

The rule this generalises to: **when parallelism already comes from one thread
per camera, every library underneath must be told to work serially.** It is
worth checking for explicitly, because it presents as "the machine is too small"
rather than as a bug.

**Locate the plate — inside the vehicle box, always.** Every government feed
carries burnt-in text: a timestamp band, a camera name, `REC`, a site label,
some of it white-on-dark at plate-like size. Run OCR on the whole frame and
`CSITMS-32` and `14-06-2026` become plate candidates — and because format
failures are *persisted and flagged* rather than dropped, they would accumulate
in `sightings` as real records rather than being quietly discarded. Confining
the search structurally, not with a filter, is the cheap fix.

**Vote.** `services/anpr/vote.py`. Two passes over every read of one tracked
vehicle:

- a *whole-string* vote — conservative, can only return a plate some frame
  actually produced;
- a *per-character* vote among reads of the modal length — this recovers a plate
  **no single frame got right**, which on a moving vehicle is the normal case
  rather than an edge case.

The consensus only wins when it normalises to a valid Indian mark and the
majority read does not, or when the slots agreed more strongly without giving up
validity. A consensus that invents an unvalidatable plate is worse than an
honest majority read.

**Persist — everything.** There is no filter in the write path. Not on
confidence, not on format validity, not on whether the plate is wanted. The
designated registration number is handed over *during* the evaluation, by which
time the vehicle has already driven past; anything discarded before then is a
trace that cannot be reconstructed. `scripts/acceptance/m3.py` asserts that
format-invalid sightings are present, because the easiest way to break this
invariant is a well-meaning quality filter that nobody notices.

---

## Timestamps

`sightings.ts` is **ingest time in UTC**, never the burnt-in overlay clock.

This is not a stylistic choice. Camera 1's overlay read `14-06-2026 00:54`,
Camera 8 read `14-06-2026 00:58` and Camera 31 read `2026-08-10 10:35`, when the
actual capture date was 18 August 2026 — two cameras about two months slow, a
third eight days slow, and none agreeing. Cross-camera journey reconstruction
depends entirely on comparable timestamps; trusting the overlay would make every
journey nonsense and fire the speed-plausibility check constantly.

`slot_offset` is recorded alongside, because the organisers' middleware replays
a synchronised 12-hour slot. Wall-clock time is correct but not reproducible —
an evaluator replaying the same slot tomorrow gets different wall-clock times
for the same vehicle. The offset locates the detection in the *footage*.

---

## What the accuracy numbers mean

Reported by `scripts/evaluate_anpr.py` against the manifest the synthetic clips
were generated from. Real footage would be better input but comes with no ground
truth, and an accuracy figure without ground truth is an impression.

- **plate accuracy** — of the sightings produced, how many were exactly right.
- **usable** — exact plus reads within edit distance 2, which is what the M5
  match tiers surface to an operator. A wanted vehicle found at distance 1 is a
  result, not a failure.
- **split by condition** — day, night and glare, never averaged. Three of the
  four real feeds observed are night scenes and two show severe headlight bloom
  that swallows whole vehicles. A single headline figure across those is an
  average of two different problems.

Two limitations, stated rather than buried:

1. The evaluator scores against the whole plate vocabulary rather than
   per-camera expectations, so it measures whether the pipeline *reads plates
   correctly*, not whether it *attributes them to the right camera*.
   Cross-camera attribution is what M4's journey test measures.
2. The generated clips are deliberately plain — a coloured body, a windscreen
   band, a plate panel. They are a controllable, repeatable signal for the
   plate-reading and voting stages; they are not a test of a vehicle detector on
   real traffic. The government feeds are that test, and they have no ground
   truth, so results on them are reported as read counts and confidence rather
   than as accuracy.

---

## Instrumentation

Per-stage p50/p95/max and per-camera throughput are rolled up in process and
written once per camera per minute (`anpr_stage_stats`, `anpr_throughput`). A
row per frame per stage would out-write the sightings themselves and mostly
measure the instrumentation.

`/api/performance` reports them, including `frames_analysed` against
`frames_decoded` — the sampler's saving, measured rather than claimed.
