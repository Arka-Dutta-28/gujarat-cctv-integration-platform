# Hosting the demo

The hosted instance runs from the build machine behind an HTTPS tunnel, with no
inbound port opened. The machine must stay on for the evaluation window; a
rented VM is the fallback (§6).

Two of the seven evaluation criteria reward a working hosted instance with test
credentials, so this is worth an hour.

---

## 1. What a hosted instance is, and how it differs

Locally the browser talks to **three** origins: the Vite dev server on 5173, the
API on 8000 and the media server on 8889. That is fine on `localhost` and wrong
everywhere else — it needs three public hostnames, three certificates and a CORS
policy, each of which can be misconfigured on the day an evaluator opens the
link.

`docker-compose.hosted.yml` collapses that to **one origin**:

```
     https://your-host/                nginx  ──►  the built React app
     https://your-host/api/…           nginx  ──►  api:8000
     https://your-host/media/…         nginx  ──►  mediamtx:8888   (HLS)
```

Three other things change with it, and each has a reason:

| Change | Why |
|---|---|
| **The web tier is a real build served by nginx** | The base image runs `vite dev` — right for development, wrong for anything a stranger can open: unminified, with a hot-reload websocket, and explicitly not meant to face a network |
| **Authentication is on** | The base file leaves it off so a clean checkout comes up with no accounts and the acceptance tests run unchanged |
| **Live video is HLS, not WebRTC** | See below — this is the one that surprises people |
| **Nothing but nginx listens** | The database, Redpanda and the media server's control API bind to the host locally because that is how you develop |

### Why HLS on a hosted instance

WebRTC's *signalling* is HTTP and goes through a tunnel perfectly. Its **media
rides UDP**, and a tunnel carries HTTP only.

So the failure is the nastiest kind: the WHEP handshake succeeds, the session
establishes, the UI says connected — and no frame ever arrives. Nothing reports
an error, because at the HTTP layer nothing went wrong.

HLS is plain HTTP over the same origin, so it simply works. The cost is a second
or two of latency, narrowed by the low-latency HLS settings already in
`infra/mediamtx/mediamtx.yml`. The player is *told* which transport it is
getting and never guesses — and `hls.js` covers every browser except Safari,
which plays HLS natively.

> On a machine with a public IP and open UDP, use `docker-compose.prod.yml`
> instead and keep WebRTC. It is the better transport whenever it can actually
> reach the browser.

---

## 2. Settings

Put these in `.env` — it is gitignored, and nothing here belongs in a commit.

```bash
AUTH_SECRET=$(openssl rand -hex 32)     # signs session tokens
PUBLIC_ORIGIN=https://cctv.example.com  # the public URL, no trailing slash
DEMO_PASSWORD=…                         # read-only account for evaluators
OPERATOR_PASSWORD=…                     # read/write account
TUNNEL_TOKEN=…                          # from step 3
```

`PUBLIC_ORIGIN` is the one that is easy to get wrong. It is where the **browser**
fetches playlists and segments, so it must be the public URL — not
`http://localhost`, not the container name. Get it wrong and the app loads,
the map draws, and video silently fails.

---

## 3. Create the tunnel

One-time, and it needs a Cloudflare account with a domain on it.

1. Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** →
   **Create a tunnel** → **Cloudflared**.
2. Name it, then copy the **token** it shows. Put it in `.env` as
   `TUNNEL_TOKEN`. It is a credential — treat it like a password.
3. Add a **public hostname**:
   - **Subdomain / domain** — whatever you want the URL to be
   - **Service** → **HTTP** → `web:80`

   `web` is the container name on the compose network; the tunnel container
   resolves it there. Only this one hostname is needed — everything else is
   behind nginx.

---

## 4. Start it

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.hosted.yml \
               -f docker-compose.tunnel.yml up -d --build

# Create the two logins from the environment. Passwords are never in a file.
docker compose -f docker-compose.yml -f docker-compose.hosted.yml \
  run --rm --entrypoint python api -m scripts.seed_accounts
```

Without the tunnel file it comes up on `127.0.0.1:8080` for local testing; set
`HOST_PORT` to change the port and `HOST_BIND=0.0.0.0` to expose it on the LAN.

---

## 5. Prove it works — from outside, logged out

**Every check below in a private window**, on a different network if you can.
The commonest hosting failure is a link that works only for the person who set
it up.

```bash
B=https://cctv.example.com

curl -s -o /dev/null -w "app      %{http_code}\n" $B/
curl -s -o /dev/null -w "spa      %{http_code}\n" $B/any/app/route   # 200, not 404
curl -s -o /dev/null -w "api auth %{http_code}\n" $B/api/cameras     # 401 — auth is on
curl -s -o /dev/null -w "docs     %{http_code}\n" $B/api/docs        # the Swagger page
```

Then, with a token:

```bash
TOKEN=$(curl -s -X POST $B/api/auth/login -H 'content-type: application/json' \
  -d '{"username":"demo","password":"…"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

CAM=…   # any online camera id from $B/api/cameras
curl -s "$B/api/cameras/$CAM/stream" -H "Authorization: Bearer $TOKEN"
```

That must return **`"protocol": "hls"`** and a URL ending `/index.m3u8`. Fetch
it — a playlist should come back as `application/vnd.apple.mpegurl`. If the
protocol says `webrtc`, `MEDIA_TRANSPORT` did not reach the relay.

**Verified locally on 31 Aug 2026**, through the full chain: master playlist →
media playlist → init segment → a 46 KB `video/mp4` segment, all 200 through
nginx on one origin.

Finally, open it in a browser and click a camera. Video should paint within a
few seconds.

---

## 5b. Without a Cloudflare domain — the quick tunnel

`docker-compose.quicktunnel.yml` needs **no account, no domain and no token**.
`cloudflared tunnel --url` dials out and is handed a random
`*.trycloudflare.com` hostname.

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.hosted.yml \
               -f docker-compose.quicktunnel.yml up -d --build

make quicktunnel-url          # read the hostname out of the logs
```

Then put that URL in `.env` as `PUBLIC_ORIGIN` and restart the one service that
needs it — `docker compose … up -d relay`. Only the relay does: the front end
uses same-origin relative URLs, so nothing is baked at build time. That is why
the URL can be discovered after the stack is already running.

**The URL changes on every restart of the tunnel container.** A link submitted
on Monday is dead on Tuesday. This is the right tool for proving the chain
works and for a demo you are present at, and the wrong one for anything printed
in a submission — that needs §3's named tunnel, which needs the account and the
domain.

Quick tunnels are also rate limited and explicitly unsupported; Cloudflare says
so in the container's own first log line.

**Do not run this overlay without the hosted one.** The hosted overlay is what
turns authentication on. A quick tunnel puts the stack on the public internet
in about six seconds, and anyone with the URL reaches it.

**Verified end to end, 6 Sep 2026**, on `securities-cindy-bio-calculated`:

| check | result |
|---|---|
| app, and a deep SPA route | 200 |
| `/api/cameras` logged out | **401** |
| `/api/docs` | 200 |
| demo login, then read | 200 |
| demo login, then **write** | **403** on cameras, watchlist and alert status |
| operator login, then the same write | 200 |
| HLS master → media playlist → segment | 200, `video/mp4`, ~40 kB each |
| stream protocol | `"hls"`, on the public origin |

Live figures off that instance a few minutes in: 80 cameras registered, 79
online, 47 processing across 2 ANPR shards, **42.7 sightings/minute**, 148
distinct plates, alert p95 3.6 s.

---

## 5c. A stable URL at zero cost — Tailscale Funnel

**Decided 7 Sep 2026: no money is spent on this project.** That rules out §3's
named tunnel, because it needs a domain on the Cloudflare account and the
account has none — Cloudflare Registrar sells at cost (`.in` and `.com` $10.46,
`.org` $8.50, `.fyi` $5.20) but at cost is still a cost.

So the submittable URL comes from **Tailscale Funnel**, which is free on every
plan and gives a hostname that survives restarts:

```
https://<machine>.<tailnet>.ts.net
```

### Why this and not the alternatives

| option | stable URL | cost | why not |
|---|---|---|---|
| Cloudflare quick tunnel | ✗ dies on restart | free | fine for a demo you attend, not for a printed link |
| Cloudflare named tunnel | ✓ | **needs a domain** | ruled out by the zero-cost decision |
| ngrok free | ✓ assigned dev domain | free | **interstitial warning page** before the site, 1 GB transfer, 20k requests, and it stops once $5 of credit is used. HLS video exhausts that in a day |
| **Tailscale Funnel** | **✓** | **free, all plans** | bandwidth is throttled and it is beta — acceptable |

The interstitial is what kills ngrok for this purpose: an evaluator clicking the
submitted link would meet a warning page, not the platform.

### The two commands

Installing needs root, so these are run by hand rather than from a session:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                 # prints a URL; approve it in the browser
sudo tailscale funnel 8090        # 8090 is the hosted overlay's nginx
```

`tailscale up` prints an authorisation URL. Opening it signs the machine into a
tailnet with an existing Google or GitHub account — no new password, no card.

**Funnel port 8090 and nothing else.** The hosted overlay puts nginx in front of
the API, the app and HLS as a single origin, which is exactly what makes one
funnelled port sufficient — and it is also the only port that has authentication
in front of it. Funnelling the API or MediaMTX directly would publish them with
no login at all.

Funnel listens only on 443, 8443 and 10000 on the public side, TLS only, so the
public URL is HTTPS with a real certificate.

### Done — 7 September 2026

**`https://aalok.tail9e8ec5.ts.net`**

Enabling it needed three things, in this order:

1. `sudo tailscale set --operator=<user>`, so `tailscale funnel` can be driven
   without root afterwards.
2. `tailscale funnel --bg 8090` prints an approval URL the first time and
   **blocks until it is visited** — it looks like a hang. Opening it enables
   HTTPS certificates for the tailnet and adds the `funnel` node attribute to
   the policy file.
3. `PUBLIC_ORIGIN` set to the `ts.net` URL and `docker compose … up -d relay`.
   Only the relay needs it; the front end uses same-origin relative URLs, which
   is why the hostname can be discovered after the stack is already running.

One consequence worth stating because it cannot be undone: enabling HTTPS
certificates writes the device name to the public Certificate Transparency
ledger permanently. That is true of any publicly trusted certificate, including
the quick tunnel's, and `aalok` carries nothing sensitive.

**Verified from the public internet:**

| check | result |
|---|---|
| app, and the `/live` SPA route | 200 |
| `/api/cameras` logged out | **401** |
| `/api/docs`, `/api/openapi.json` | 200 |
| demo login → read | 200 |
| demo login → **write** | **403** |
| stream resolution | `"hls"`, URL on the funnel origin |
| HLS master → variant → segment | 200, `video/mp4`, **54 kB** |
| logged-out login screen | renders, console clean |
| signed-in app | map, 80 cameras / 79 online, 49 live alerts, 171,445 sightings |
| journey query | **50 ms** against a 2 s budget |
| **URL after restarting the container behind it** | **unchanged, still 200** |

That last row is the whole point of the exercise, and the thing the quick tunnel
could not do.

`tailscaled` is enabled at boot and the funnel config persists in its state, so
the URL also survives a reboot of the host. What it does not survive is the host
being switched off, which is the one cost that is not money.

**A hosted instance is not verified until a browser has rendered it**: every curl
check passed on 6 September while the app was a blank page, because none of them
ran JavaScript. Both the logged-out and signed-in views were rendered here.

### What is still true of any of these

The machine has to stay awake through the evaluation window. That is the cost
that is not money, and it is the same for the quick tunnel, the named tunnel and
Funnel alike.

---

## 6. If you rent a VM instead

The deploy is the same; that is the point of doing it in containers.

- Use `docker-compose.prod.yml` rather than `docker-compose.hosted.yml`, and set
  `PUBLIC_WEB_ORIGIN`, `PUBLIC_API_ORIGIN`, `PUBLIC_MEDIA_ORIGIN` and
  `PUBLIC_MEDIA_HOST` to the public hostnames.
- WebRTC then works, and is lower latency.
- You need TLS certificates for those hostnames, and UDP open for WebRTC media.
- Size it from the measured figure: **~50–57 cameras per 20-core box**. The
  demo estate is 50 simulated cameras, so one reasonably sized box carries it.

---

## 7. Before you hand over the link

- [ ] Open it in a **private window, logged out**, on another network
- [ ] The demo account is **read-only** — confirm it cannot change anything
- [ ] No real or personal data in the seeded estate
- [ ] `/api/docs` loads — it answers "documented standard APIs" and the
      integration-ready-APIs bonus for no extra work
- [ ] Video plays, and the plate search returns a route
- [ ] The machine will not sleep. Check the power settings, then check them again

---

## 8. Known gaps

Stated here rather than discovered later:

- **The tunnel is only as available as the machine.** No cached copy, no
  failover. This is the trade `docker-compose.tunnel.yml` makes explicit.
- **`TUNNEL_TOKEN` is a credential.** It lives in `.env`, which is gitignored.
  It must never reach a commit, a screenshot or a demo video.
- **HLS adds a second or two of latency** against WebRTC. Worth saying out loud
  in the video rather than letting a judge notice it.
