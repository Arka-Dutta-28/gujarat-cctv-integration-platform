# Deploying the platform

The stack runs anywhere Docker does. This is the whole procedure for a hosted
instance — the one an evaluator reaches from a browser — and the last section is
what to check when something does not work, because on demo day the useful
document is the one that says what to look at.

Everything below assumes a machine with **4 vCPU and 16 GB RAM** for the
platform, and more if the ANPR tier is to carry many cameras: the measured
figure is **50–57 cameras per 20-core box**, so scale `--scale anpr=N` and the
host to the camera count rather than hoping.

---

> **For a public instance, read `docs/hosting.md` instead.** It covers the
> single-origin deployment, the Cloudflare Tunnel, and why a hosted instance
> plays video over HLS rather than WebRTC. This document is the general deploy
> guide and the multi-origin (VM with a public IP) path.

## 1. Before you start

You need three things this repository deliberately does not contain:

| | |
|---|---|
| A host | Docker with Compose v2. Ports 80/443 reachable. |
| A domain with TLS | Terminate TLS at a reverse proxy (Caddy and nginx both fine). The platform speaks plain HTTP behind it. |
| Passwords | `POSTGRES_PASSWORD`, `AUTH_SECRET`, `DEMO_PASSWORD`, `OPERATOR_PASSWORD`. None has a default and none is ever committed. |

---

## 2. Configure

```bash
git clone <repository> && cd guj
cp .env.example .env
```

Set these in `.env`. The first four are secrets; the last four are how the
browser reaches each component and are the ones most often got wrong.

```bash
POSTGRES_PASSWORD=…                      # no default; compose hard-fails without it
AUTH_SECRET=$(openssl rand -hex 32)      # signs session tokens
DEMO_PASSWORD=…                          # the evaluator's read-only login
OPERATOR_PASSWORD=…                      # the read/write login

PUBLIC_WEB_ORIGIN=https://cctv.example.gov.in
PUBLIC_API_ORIGIN=https://api.cctv.example.gov.in
PUBLIC_MEDIA_ORIGIN=https://media.cctv.example.gov.in
PUBLIC_MEDIA_HOST=media.cctv.example.gov.in
```

> **`PUBLIC_MEDIA_HOST` is the one that will bite you.** Left unset, the media
> server advertises its Docker bridge IP as a WebRTC candidate. A remote browser
> can route to that address, so ICE *looks* valid and then starves — measured
> here as 66 STUN requests for 2 responses, which presents as a video that
> connects and never paints. It must be an address the viewer's browser can
> actually reach.

---

## 3. Start it

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
make migrate                 # forward-only; refuses to run if an applied file changed
make seed                    # cameras, corridor, watchlist, planted demo plates
make seed-accounts           # creates `demo` (viewer) and `operator` from the env
```

The production overlay turns authentication **on**, publishes only the web app
and the API, and stops the database and Redpanda binding to the host at all.

Analytics are a separate scale unit and are started separately:

```bash
ANPR_DOCKERFILE=services/anpr/Dockerfile.gpu ANPR_SHARDS=6 \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  up -d --build --scale anpr=6 anpr
```

Each worker claims a shard through a Postgres advisory lock, so `--scale` is the
entire horizontal-scaling command — no orchestrator, no leader election, no
per-replica configuration. Confirm the estate is fully claimed:

```bash
docker compose logs anpr | grep owns
```

Road-snapped journeys need OSRM. It is behind a profile because preparing the
extract takes a few minutes and ~219 MB, and the platform degrades to
straight-line distances without it rather than failing:

```bash
make osrm-prepare
docker compose --profile routing up -d osrm
```

---

## 4. Prove it works

```bash
make accept-m8 -- --api https://api.cctv.example.gov.in \
                  --web https://cctv.example.gov.in
```

Seven checks: a logged-out caller is refused every data endpoint, `/health` and
Swagger stay public, the demo account signs in as a viewer, reads everything,
and is refused every write.

Then, from an incognito window, walk the demo:

1. **The map** — 80 cameras (50 simulated, 30 government), the estate's status
   legend, "indexing since HH:MM".
2. **Click a camera** — live video in about a quarter of a second.
3. **Trace `GJ18TR4321`** — the planted corridor vehicle. Route, movement
   history, per-hop speeds, and Export PDF. *Use this plate, not the alerting
   one: background plates ride a looping clip shared by distant cameras, so
   their journeys are correctly flagged as implausible.*
4. **Watchlist `GJ05UV9972`** in the alert console — the next sighting raises an
   alert with its crop, about a second after the vehicle is read.
5. **`/#/performance`** — the live figures, including the ones that are not
   flattering.

---

## 5. When something is wrong

Read these in order. Each has been the answer at least once here.

**The map is blank.** The tile host is unreachable from the browser (a
firewalled control room does this). Camera pins still render — layers are built
on `style.load`, not `load`, precisely so a missing basemap does not hide the
data.

**A camera shows "offline".** Believe it. The health prober reports what it can
reach, never what the upstream claims about itself; two of the government feeds
report `"status":"live"` while returning HTTP 500 on every request. The live
view refuses to attempt playback on an offline camera and says when it last
answered.

**Video connects and never paints.** Almost always `PUBLIC_MEDIA_HOST`. If the
camera instead shows "upstream unstable", the relay is dying and restarting —
that is the source, and the panel says how many times in the last minute.

**Sightings are not accumulating.** `GET /api/performance` → `write_health`.
It reports what the pipeline *produced* against what actually reached the index,
and names an invariant-1 breach in words. This exists because a parameter-order
bug once failed every write in the estate for two hours while the throughput
counters looked perfectly healthy.

**Accuracy looks low.** Check `GET /api/cameras/anpr-capability` first. Of the
80 cameras here, 30 are wide-area government views whose plate crops average
66 px — about 7 px per character — and no recogniser resolves a plate at that
scale. The
accuracy figure is scoped to the ANPR-grade cameras and should never be quoted
estate-wide.

**Throughput is disappointing.** Read the per-stage latencies before assuming
saturation. A stage that disagrees with the same stage probed alone is measuring
contention, not cost: the largest single finding in this build was that every
library underneath the pipeline sized its thread pool from the core count while
parallelism already came from one decode thread per camera, and one
configuration line moved accuracy from 10.2% to 75.3%.

**OCR shed rate is high.** Expected above ~57 cameras on a 20-core box, and it
is on the performance page for that reason. Raising `ANPR_OCR_CONCURRENCY` makes
it worse — measured: shed went 84.5% → 96.9%. Add hosts, not threads.

---

## 6. Backup and retention

- **The database** carries everything that matters: the registry, every
  sighting, the watchlist, alerts and the audit trail. `pg_dump` it.
- **Evidence crops** live on the `anpr-crops` volume, roughly 8 kB per sighting
  (~48 MB/day at this estate's rate). `services/anpr/crops.py:prune()` deletes
  by day. Retention on the images is separate from retention on the rows: the
  trace still works after a crop has aged out, and the record says the crop is
  gone rather than pretending there never was one.
- **No video is stored anywhere.** It stays at the camera and is pulled on
  demand, which is the platform's edge-first claim made real.
