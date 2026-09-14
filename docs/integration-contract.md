# The live camera grid — contract, and where each clause is discharged

**Source:** *Consuming the Sentinel Camera Grid* (integration reference, Sentinel
sandbox), published on the hackathon Resources page. It replaced the earlier
"live camera feed" entry, which was a URL and nothing else.

**Why this document exists.** Every clause in that reference describes a failure
that is *silent*. A pipeline reading UDP does not crash, it produces torn frames
that look like model bugs. A tracker timing by arrival does not crash, it
computes impossible velocities on every connection. A client that aborts on the
first decoder complaint does not crash, it bounces on H.265 streams for ever
while reporting reconnects. None of these show up as an error; they show up as
worse numbers, and there is no second attempt at the evaluation to notice
during. So each clause is written down here against the code that discharges it
and the test that pins it.

---

## 1. What we are connecting to

The grid is **live RTP/RTSP**. One second of video takes one second to arrive,
frames carry monotonic presentation timestamps, and there is no seeking and no
running ahead of real time. Each endpoint behaves like a physical camera on an
operational network.

| Protocol | Shape | Intended for |
|---|---|---|
| RTSP | `rtsp://<host>:8554/stream/<id>` | AI inference |
| WebRTC (WHEP) | `http://<host>:8889/stream/<id>/whep` | low-latency browser preview |
| HLS | `http://<host>/live/stream/<id>/index.m3u8` | dashboards, restricted networks |

Those URL shapes are recorded here for orientation **and are not used anywhere in
the platform**. The reference is explicit:

> Camera ids and the set of available cameras can change; the catalogue is the
> contract, the URL pattern is not.

Everything starts from `GET /api/ingest`.

---

## 2. The pre-submission checklist, mapped

| # | Checklist item | Where it is discharged | Test |
|---|---|---|---|
| 1 | Every client forces RTSP over TCP | [`capture.ffmpeg_options_for`](../services/anpr/capture.py) sets `rtsp_transport;tcp` around every `VideoCapture` construction; the relay passes `-rtsp_transport tcp` on input and output; the simulator does the same | `TestTransportOptions`, `test_relay.py` |
| 2 | No timing logic depends on `CAP_PROP_FPS` or arrival time | [`capture.PtsClock`](../services/anpr/capture.py) is the only clock the pipeline sees. The declared rate is read once, recorded, and used for nothing | `TestPtsClock`, `TestMeasuredRate` |
| 3 | Inter-frame gaps do not crash or stall the pipeline | A gap is a larger PTS delta and nothing else. Failed reads are tolerated and retried rather than ending the session; only `STALL_TIMEOUT_S` with no frame at all ends one | `TestJoinTolerance` |
| 4 | Reconnect with backoff, tested by restarting a feed | [`capture.Backoff`](../services/anpr/capture.py) — 2 s base, 30 s cap, full jitter, reset after a healthy session | `TestBackoff` |
| 5 | Decoder warnings on join are logged, not fatal | The join window tolerates up to `JOIN_GRACE_READS` failed reads before the first frame arrives, `STEADY_GRACE_READS` afterwards | `TestJoinTolerance` |
| 6 | Camera list and per-camera properties read from `/api/ingest` | [`adapters/catalogue.py`](../services/adapters/catalogue.py) + [`scripts/seed_real_feeds.py`](../scripts/seed_real_feeds.py); stored by migration `013_stream_properties` | `test_catalogue.py` |
| 7 | Pipeline handles mixed H.264/H.265 and mixed resolutions | Nothing decodes by codec — FFmpeg does. The one place a resolution assumption lived, the minimum vehicle area, is now a **fraction of the frame** | `test_pipeline.py`, `min_vehicle_area` |
| 8 | Behaviour is sane across a scene discontinuity | PTS regression, PTS jump, reconnect and whole-frame content cut all raise `Frame.discontinuity`; the worker retires scene state on it | `TestPtsClock`, `TestReconnectIsADiscontinuity` |

---

## 3. The clauses in detail, and what each one changed

### Force RTSP over TCP

> UDP is accepted but fails across NAT and most corporate firewalls. Partial UDP
> delivery produces corrupt frames that look like model bugs.

OpenCV exposes no API for transport selection; FFmpeg reads it from
`OPENCV_FFMPEG_CAPTURE_OPTIONS` **at `VideoCapture` construction**. That is a
process-global environment variable, so two camera threads opening at the same
instant can read each other's options — the capture layer holds a lock across
the constructor and restores the previous value after.

**What changed:** the ANPR worker previously opened `cv2.VideoCapture(url,
cv2.CAP_FFMPEG)` with no options at all, and therefore negotiated UDP wherever
the server offered it. The relay was already correct; the analytics path, which
is the one being graded on accuracy, was not.

The reference also notes that a network blocking 8554 should use HLS instead.
That is why the endpoint ladder exists rather than a single URL.

### Drive all timing from PTS, never from arrival time

> When a client connects, the gateway replays its buffered group-of-pictures so
> the decoder can start at a keyframe. The first second or two of frames may
> therefore arrive faster than real time. A tracker that timestamps by arrival
> will compute impossible velocities immediately after every connection.

This is the most consequential clause for this platform specifically, because
the platform already has a rule that acts on impossible velocities: a journey
whose implied speed exceeds ~120 km/h is split and its confidence lowered, on
the grounds that this is how cloned plates surface. Timing by arrival would have
made the platform's own reconnects look like cloned plates — a false positive
generated by the client, in the one analysis an evaluator is most likely to look
at closely.

`PtsClock` does three things:

- **decides whether PTS is usable at all**, since some backends return 0.0 for
  ever. A reading of exactly 0.0 is ambiguous — it is what a stream at its first
  frame reports *and* what a dead backend reports — so the question stays open
  until the reading either advances or fails to across enough frames. Falling
  back is logged and counted, never silent, because "which clock was this camera
  on" changes how much its dwell times are worth;
- **detects discontinuities**, and keeps the timeline monotonic across them;
- **anchors media time to wall-clock time** by the running minimum of
  `wall_at_read - media_elapsed`. The minimum is the right estimator: the join
  burst makes early frames look late by however deep the buffer was, and each
  later observation of a genuinely live frame is closer to the truth.

**What that anchor bought.** Sightings used to be stamped `datetime.now()` at the
moment the harvest loop got round to the finished track — some seconds after the
vehicle left frame, and more under load. They are now stamped with the vehicle's
own last-seen instant, mapped through the anchor. That is the timestamp column
in the government-feed output report, and the one a cross-camera journey is most
sensitive to.

### Do not assume a constant frame rate

Everything downstream is driven by elapsed PTS between frames, so a gap is
simply a longer delta. The adaptive sampler compares elapsed media time against
a target rate and never counts frames.

### Reconnect automatically, with backoff

Full jitter rather than plain doubling, because 80,000 cameras behind a
supervisor that restarts a rack at once is a thundering herd, and the
randomisation is what spreads the reconnects. A session that ran healthily for
30 s resets the backoff, so a single blip does not push a working camera onto a
30-second cadence.

### Decoder complaints at join are not failures

> Attaching mid-stream can produce decoder messages such as `Error constructing
> the frame RPS` until the first IDR frame arrives. This is normal and
> self-corrects.

The old loop ended the session on the **first** `read()` returning false, waited
five seconds, and reconnected — which on an H.265 stream is an infinite loop of
five-second reconnects that never reaches an IDR, while every log line says the
camera is fine. Failed reads are now counted, tolerated generously before the
first frame and less generously after, and reported in the metrics so the
tolerance itself is visible rather than hidden.

### The grid is not uniform

> Cameras differ in resolution, codec, frame rate, and bitrate. A fixed-shape
> inference batch across every camera will not work unscaled.

Per-camera properties are read from the catalogue and stored on `cameras`
(migration `013`). The concrete assumption this removed was
`MIN_VEHICLE_AREA_PX = 2_000`, commented "tuned for 1080p": on a 640×480 feed
that is 0.65% of the frame — a vehicle well worth reading — and on 4K it is
0.024%, a smudge no OCR can use. The same constant therefore skipped readable
vehicles on the small cameras and wasted OCR on unreadable ones on the large
cameras, simultaneously. It is now 0.1% of frame area with an absolute floor,
recomputed per frame because a stream can change resolution after a restart.

### Expect a scene discontinuity

> Each feed is a continuous recording that loops. At the loop point the scene
> cuts abruptly, similar to a camera reboot. Long-lived state — background
> models, re-identification galleries, object track ids — should recover from a
> hard cut rather than assuming infinite continuity.

Detected two ways, because a loop can restart the media clock or not:

- **timeline** — PTS running backwards by more than a second, or jumping forward
  by more than a minute; and every reconnect, by definition;
- **content** — the frame differencing the pipeline already computes for motion,
  read at a much higher threshold. A lorry filling the view is nowhere near it;
  a different scene entirely is well past it. This costs nothing extra: the
  arithmetic was already being done for the sampler.

On a discontinuity the worker:

| state | action | why |
|---|---|---|
| open tracks | **completed and written** | a vehicle in view at the loop point was a real vehicle and its reads are real reads. Discarding them would lose sightings — the one thing this platform must never do |
| tracker ids | cleared | so a car in the new scene does not inherit a car from the old one |
| tamper reference | cleared | the established view has legitimately changed; comparing against the old one reports a moved camera every single loop |
| motion baseline | cleared | so the first frame of the new scene is not read as the whole frame moving |
| overlay filter | **kept** | burnt-in furniture is a property of the camera, and the camera has not changed |

### Do not plan around obtaining copies of the footage

> There is no file download… `/stream/<id>` is the browser playback fallback: it
> answers range requests for a media player, so pulling it with a plain curl or
> wget yields a partial file that looks complete.

Nothing in the platform downloads footage. Analytics read a live capture; the
relay republishes with `-c copy` and stores nothing; evidence retained is a JPEG
crop per sighting, not video. The `mediasrc` byte-range server exists only to
serve the locally generated synthetic clips that stand in for the estate in dev.

### Do not publish to the gateway; pace your load

Consume only — the platform never pushes to an upstream path and never calls the
gateway's control API. Each connected client gets its own copy of the stream, so
the worker opens a camera only when a shard owns it, and the relay's pull feeds
both the operator watching and the analytics reading rather than each opening
its own connection.

---

### Access as of 14 September 2026

| | How |
|---|---|
| Sign-in | `POST /auth/login` with `email` and `password` (the `XXXX-XXXX-XXXX` access code); session cookie `sentinel` |
| Catalogue | `GET /cameras.json` → `[{id, name}]`, 30 cameras (`/api/ingest` is gone) |
| Video for the platform | `rtsp://103.250.160.189:8554/stream/<id>` over TCP, **with the email and access code as the RTSP login**. 1920×1080 H.264 |
| Video over HLS | `/<id>/index.m3u8` answers `403 browser required` to non-browser clients. Not used, and not worked around |

## 4. The catalogue client, and why it is deliberately shape-tolerant

The reference names the fields the catalogue carries — id, location, codec, live
status, stream properties, three URLs — but does not pin their JSON spelling.
The sandbox has already changed shape once: it used to serve per-camera
`/api/cameras/<n>/state` documents with a single `stream_url`, and it now serves
one catalogue with three URLs per camera.

So the client **discovers** rather than asserts:

- the camera list is found under any of the plausible envelope keys, or as a
  bare list, or as a mapping keyed by camera id;
- scalar fields are looked up under a list of aliases, case- and separator-folded,
  so `frameRate`, `frame_rate` and `frame-rate` are one field;
- endpoints are found by walking the record for anything URL-shaped and
  classifying it **by what it is** — `rtsp://` is RTSP, a path ending `.m3u8` is
  HLS, a path ending `/whep` is WHEP — rather than by the key it was found under;
- relative paths are resolved against the base;
- unrecognised fields are carried through in `raw` rather than dropped.

That tolerance is not slack. The alternative is a client that breaks on the
morning of the evaluation because a key was renamed.

**WHEP is never handed to a decoder.** It is a browser transport requiring an SDP
negotiation, and giving it to FFmpeg produces a confusing failure rather than
video. It is stored, and offered to the browser.

---

## 5. Onboarding is now a sync, not a seed

`scripts/seed_real_feeds.py` reconciles the registry against the catalogue and
can be re-run at any time.

- **Enumerates from the catalogue.** The previous version walked
  `/api/cameras/1..31/state` — a hardcoded URL pattern *and* a hardcoded estate
  size, which fails silently: a renumbered grid onboards the wrong cameras and
  reports success.
- **Never deletes.** A camera absent from the catalogue is marked `offline` and
  keeps its row, because `sightings` references it and those rows are evidence.
- **Refuses to retire the estate on an empty answer.** "The catalogue is
  unreachable" and "the grid has no cameras" call for completely different
  operator responses, and collapsing them would let one bad request mark every
  camera offline.
- **Positions by place name.** The old table was keyed by camera *number* — the
  one key the reference says can change. See below.
- **Never overwrites a surveyed position.** Once an operator enters real
  coordinates, `geo_precision = 'survey'` and the sync leaves the geometry alone.

### Positioning, and the `geo_precision` column

The catalogue gives a location name and no coordinates. `services/common/gazetteer.py`
resolves the name through a JSON gazetteer in three tiers — landmark, city,
district centroid — and flags anything it cannot place as `unplaced` rather than
guessing. The tier is stored on the camera, because coverage analysis and gap
reports must not treat a district centroid as a surveyed position, and an
operator needs to see which pins still need one.

Matching is on whole words. Substring matching would put every camera whose name
contains "una" — Junagadh, Punagam — in Una, Gir Somnath.

---

## 6. Heavy models, held in reserve

The reference does not ask for this; the problem statement's bonus criteria do,
and the grid's heterogeneity is what makes it necessary.

The platform's default stages are the cheap ones, and that default is measured:
the classical plate locator matched the learned detector's read rate at 1/60th
of the cost, and Tesseract beat the available hub recogniser by a factor of
seventy-five on Indian plates. But a default chosen on average is wrong
somewhere, and on this grid the somewheres are predictable — an oblique angle,
sodium light, a dirty dome, a resolution the classical locator's contrast
assumptions do not survive. On such a camera the light path reports itself
perfectly healthy: vehicles tracked, frames analysed, **zero plates**.

`services/anpr/escalation.py` watches each camera's own read rate and moves that
camera — and only that camera — up a ladder:

1. OCR → learned recogniser (~4x)
2. plate locator → learned detector (~60x)
3. vehicle stage → YOLO (~15x)

One rung at a time, so the record says *which stage* was the problem. Cheapest
hypothesis first, which is also the right diagnostic order: if the locator is
finding plate-shaped regions the OCR cannot read, swapping the locator changes
nothing. An escalation that does not beat the light path by a real margin is
**rolled back**, and the camera is flagged as needing a human — because a camera
pointed at a wall reads nothing on any model, and paying 60x for that for ever
is how an estate migrates onto its most expensive configuration in pursuit of
plates that are not there. A per-process budget caps how many cameras may run
heavy at once, for the same queueing reason that bounds OCR concurrency.

The heavy models are pre-warmed even when the run defaults to light, so an
escalation never pays a cold model download inside a decode thread — which would
land on the camera that was already reading badly enough to need help.

---

## 7. Regenerating the derived data

```bash
make onboard          # sync the registry from the catalogue
make onboard-dry      # show what the sync would do, write nothing
make confusion        # regenerate the OCR confusion table from glyph similarity
```

The OCR confusion table (`data/ocr-confusion.json`) is derived rather than typed
in — see `services/common/confusion.py`. Glyph mode needs no data and works
before a camera is onboarded; the empirical mode counts substitutions from real
reads against ground truth and supersedes it. The built-in prior remains as a
floor so normalisation never depends on a generated file being present.

---

## 8. What is still assumed, stated plainly

- **Coordinates are inferred from place names.** They are approximate to the
  locality. The `geo_precision` column says so per camera; a real deployment
  takes them from the department asset register, which is what the registry's
  import API is for.
- **Bearing, field of view and range are left null** on catalogue-onboarded
  cameras. They drive coverage polygons, and invented values produce
  confidently wrong coverage.
- **The glyph confusion table is a proxy.** It ranks costs better than a hand
  table can and covers pairs a hand table missed, but rendered in the generic
  grotesques available on a build machine it also gets things wrong — it ranks
  `Q` above `O` as the letter a `0` was. That is why it is merged over the
  prior rather than replacing it, and why the empirical mode exists.
- **PTS is trusted where the stream provides it.** A stream that reports
  plausible but wrong timestamps would mislead dwell and speed. The clock
  reports which source it used, per camera, so the claim can be checked.
