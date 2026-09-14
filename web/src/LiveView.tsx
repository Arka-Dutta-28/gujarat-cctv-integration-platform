/**
 * Live video for one camera.
 *
 * The player knows nothing about how the camera delivers video. It asks the
 * API "how do I watch this?" and gets back a protocol and a URL — RTSP,
 * progressive HTTP and ONVIF all arrive here as WebRTC because the platform
 * pulled them into the media server. That is the whole point of the adapter
 * spine: one player, any camera.
 *
 * WebRTC is spoken directly rather than through the media server's own player
 * page, for two reasons. An iframe cannot tell us when the first frame
 * actually painted, and time-to-first-frame is a graded number — M2's
 * acceptance is "clicking a pin plays video in under 3 seconds", so the UI
 * measures it and shows it rather than asserting it.
 *
 * **Two transports, chosen by the deployment, not by this file.** WebRTC needs
 * the browser to reach the media server directly, because its media rides UDP.
 * Behind an HTTP-only path — a tunnel, a corporate proxy, the "restricted
 * network" the integration contract names — the WHEP handshake succeeds and
 * then no frame ever arrives, which is the worst way for it to fail. So a
 * hosted instance sets `MEDIA_TRANSPORT=hls` and the API hands back a playlist
 * instead. The player is told which it got; it never guesses.
 *
 * HLS needs `hls.js` everywhere except Safari. `video.src = '…m3u8'` plays
 * natively on Safari and silently does nothing on Chrome and Firefox, so the
 * native path is taken only where it is actually supported.
 */

import Hls from 'hls.js'
import { useEffect, useRef, useState } from 'react'
import { fetchStream, toIST } from './api'
import type { CameraFeature, StreamTarget } from './api'

/** A relay takes a moment to start. Poll rather than fail — but not forever. */
const READY_RETRY_MS = 400
const READY_TIMEOUT_MS = 12_000

/**
 * Everything is on the local network or the same host, so host candidates are
 * all we need and they gather in milliseconds. Waiting for a full gathering
 * cycle would spend the entire 3-second budget on a STUN round trip.
 */
const ICE_GATHER_TIMEOUT_MS = 500

/**
 * A live view that has failed once should try again rather than sit there.
 * Sources genuinely blink — an upstream restarts, a relay is recycled, a
 * network hiccups — and an operator watching a camera during an incident
 * should not have to notice and click. Bounded, because a camera that is
 * really down must eventually say so instead of retrying for ever.
 */
const MAX_RECONNECTS = 3
const RECONNECT_DELAY_MS = 1500

type Phase = 'resolving' | 'starting' | 'connecting' | 'playing' | 'reconnecting' | 'error'

const PHASE_TEXT: Record<Phase, string> = {
  resolving: 'Resolving stream…',
  starting: 'Pulling the camera…',
  connecting: 'Connecting…',
  playing: 'Live',
  reconnecting: 'Reconnecting…',
  error: 'Unavailable',
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms)
    signal.addEventListener('abort', () => { clearTimeout(t); reject(signal.reason) }, { once: true })
  })
}

async function iceGathered(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === 'complete') return
  await new Promise<void>((resolve) => {
    const done = () => { clearTimeout(timer); pc.removeEventListener('icegatheringstatechange', check); resolve() }
    const check = () => { if (pc.iceGatheringState === 'complete') done() }
    const timer = setTimeout(done, ICE_GATHER_TIMEOUT_MS)
    pc.addEventListener('icegatheringstatechange', check)
  })
}

/**
 * WHEP: offer/answer over one HTTP POST. Non-trickle — the offer is sent once
 * gathering has settled, which keeps this to a single round trip.
 *
 * The connection is created by the caller and passed in, so that tearing the
 * view down always has something to close. Creating it here would leave it
 * orphaned whenever the component unmounted mid-handshake — which React does
 * on every mount in development — and an orphaned session keeps pulling video.
 *
 * Video only. These are public-space cameras: requesting an audio track we
 * have no reason to record would be a privacy decision made by accident, and
 * a video-only source answers the audio m-line with a track that never
 * carries data.
 */
/**
 * Safari (and iOS in general) plays HLS from a plain `<video src>`; nothing
 * else does. Feature-detected rather than sniffed for the browser, because the
 * question really is "can this element play this type".
 */
function canPlayHlsNatively(video: HTMLVideoElement): boolean {
  return video.canPlayType('application/vnd.apple.mpegurl') !== ''
}

/**
 * Attach hls.js to the element and report fatal errors through the same path
 * as every other failure.
 *
 * The tuning is for a live wall, not for a film. `lowLatencyMode` follows the
 * media server's low-latency HLS configuration; the short buffer and the
 * `liveSyncDuration` keep the player near the live edge rather than letting it
 * drift back a comfortable ten seconds — an operator watching a junction needs
 * *now*, and a player that quietly falls behind is worse than one that stalls,
 * because nothing on screen says it is showing the past.
 *
 * Non-fatal errors are left alone: hls.js recovers from most of them on its
 * own, and surfacing every recovered network hiccup would make a working feed
 * look broken.
 */
function attachHls(
  video: HTMLVideoElement,
  url: string,
  onFatal: (message: string) => void,
): Hls | null {
  if (!Hls.isSupported()) {
    onFatal('this browser cannot play HLS')
    return null
  }
  const hls = new Hls({
    lowLatencyMode: true,
    backBufferLength: 10,
    liveSyncDuration: 2,
    maxLiveSyncPlaybackRate: 1.5,
  })
  hls.on(Hls.Events.ERROR, (_event, data) => {
    if (!data.fatal) return
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      hls.startLoad()
      return
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
      hls.recoverMediaError()
      return
    }
    onFatal(data.details ?? 'stream failed')
  })
  hls.loadSource(url)
  hls.attachMedia(video)
  return hls
}

async function whepConnect(
  pc: RTCPeerConnection, base: string, video: HTMLVideoElement, signal: AbortSignal,
): Promise<string | null> {
  const stream = new MediaStream()
  pc.addTransceiver('video', { direction: 'recvonly' })
  pc.ontrack = (ev) => {
    stream.addTrack(ev.track)
    if (video.srcObject !== stream) video.srcObject = stream
  }

  await pc.setLocalDescription(await pc.createOffer())
  await iceGathered(pc)
  if (signal.aborted) return null

  const res = await fetch(`${base.replace(/\/$/, '')}/whep`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/sdp' },
    body: pc.localDescription?.sdp ?? '',
    signal,
  })
  if (!res.ok) throw new Error(`media server refused the stream (HTTP ${res.status})`)
  const location = res.headers.get('Location')
  await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() })
  // The session resource, so teardown can delete it. Closing the peer
  // connection alone leaves the server holding a reader that no longer exists,
  // which keeps the on-demand relay alive for a camera nobody is watching.
  return location ? new URL(location, base).href : null
}

export function LiveView({ camera, caseRef }: { camera: CameraFeature; caseRef?: string }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [phase, setPhase] = useState<Phase>('resolving')
  const [target, setTarget] = useState<StreamTarget | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ttfbMs, setTtfbMs] = useState<number | null>(null)
  const [attempt, setAttempt] = useState(0)

  const cameraId = camera.properties.id

  /**
   * The registry already knows this camera is not answering, so there is
   * nothing to gain by opening a peer connection to find out again.
   *
   * Found by using it: during an upstream outage every government feed was
   * `offline`, and clicking one spent four reconnect attempts before saying
   * "disconnected" — which reads as *the platform is broken* rather than *the
   * camera is down and we already told you*. The health prober's verdict is the
   * better answer and it is available before the first request.
   */
  const offline = camera.properties.status === 'offline'

  // A different camera starts its retry budget over.
  useEffect(() => { setAttempt(0) }, [cameraId])

  useEffect(() => {
    if (offline) return
    const ac = new AbortController()
    const started = performance.now()
    // Created up front, not inside the handshake, so the cleanup below can
    // always close it however early the view is torn down.
    const pc = new RTCPeerConnection({ iceServers: [] })
    let session: string | null = null
    let hls: Hls | null = null

    setPhase(attempt === 0 ? 'resolving' : 'reconnecting')
    setTarget(null); setError(null); setTtfbMs(null)

    let retryTimer: number | undefined

    /** Try again, or stop and say why. */
    const fail = (message: string) => {
      if (ac.signal.aborted) return
      if (attempt < MAX_RECONNECTS) {
        setPhase('reconnecting')
        setError(message)
        retryTimer = window.setTimeout(() => setAttempt((a) => a + 1), RECONNECT_DELAY_MS)
        return
      }
      setPhase('error')
      setError(message)
    }

    async function run() {
      // Resolve, retrying while a cold relay warms up. `ready: false` is a
      // "not yet", not a failure — treating it as an error would make the
      // first view of every pull-on-demand camera look broken.
      const deadline = performance.now() + READY_TIMEOUT_MS
      let resolved: StreamTarget | null = null
      for (;;) {
        const t = await fetchStream(cameraId, 'operator', caseRef)
        setTarget(t)
        if (t.ready) { resolved = t; break }
        if (performance.now() > deadline) throw new Error(t.detail ?? 'stream did not start')
        setPhase('starting')
        await sleep(READY_RETRY_MS, ac.signal)
      }

      const video = videoRef.current
      if (!video) return
      setPhase('connecting')

      if (resolved.protocol === 'webrtc') {
        session = await whepConnect(pc, resolved.url, video, ac.signal)
      } else if (resolved.protocol === 'hls' && !canPlayHlsNatively(video)) {
        hls = attachHls(video, resolved.url, fail)
      } else {
        // Safari plays HLS natively, and a `file` source is just a video file.
        video.src = resolved.url
      }
      await video.play().catch(() => { /* autoplay policy; the control stays */ })
    }

    // A WebRTC session that fails after the handshake has no HTTP error to
    // report, so without this the panel would sit on "Connecting…" for ever.
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed') {
        fail('stream dropped')
      }
    }

    const onPlaying = () => {
      setPhase('playing')
      setTtfbMs((prev) => prev ?? Math.round(performance.now() - started))
    }
    const video = videoRef.current
    video?.addEventListener('playing', onPlaying)

    run().catch((err) => {
      if (ac.signal.aborted) return
      fail(err instanceof Error ? err.message : String(err))
    })

    return () => {
      window.clearTimeout(retryTimer)
      ac.abort(new DOMException('camera changed', 'AbortError'))
      video?.removeEventListener('playing', onPlaying)
      pc.close()
      // Destroyed before the element is reset: hls.js holds its own media
      // source and buffers, and leaving them attached leaks a worker and a few
      // MB per camera switch — which, on a video wall, is every few seconds.
      hls?.destroy()
      // `keepalive` so the session is still released when this fires during
      // page unload rather than a component swap.
      if (session) void fetch(session, { method: 'DELETE', keepalive: true }).catch(() => {})
      if (video) { video.srcObject = null; video.removeAttribute('src'); video.load() }
    }
  }, [cameraId, caseRef, attempt, offline])

  if (offline) {
    const lastSeen = camera.properties.last_seen
    return (
      <div className="live">
        <div className="live-frame">
          <div className="live-overlay live-offline">
            Camera offline
            <div className="hint">
              The health prober cannot reach this feed, so there is nothing to
              play. Its sightings are still indexed and searchable.
            </div>
            <div className="hint">
              {lastSeen
                ? `Last answered ${toIST(lastSeen)} IST`
                : 'Not seen answering since the platform started'}
            </div>
          </div>
        </div>
        <div className="live-meta">
          <span className="live-badge live-error">offline</span>
          <span className="muted">
            {camera.properties.adapter} · status from the prober, not from the
            upstream&apos;s own claim
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="live">
      <div className="live-frame">
        <video
          ref={videoRef}
          data-testid="live-video"
          autoPlay
          muted
          playsInline
          controls={phase === 'playing'}
        />
        {phase !== 'playing' && (
          <div className={`live-overlay${phase === 'error' ? ' live-error' : ''}`}>
            {PHASE_TEXT[phase]}
            {error && <div className="hint">{error}</div>}
            {/* Name the upstream when it is the upstream. Retrying against a
                feed that resets every two seconds looks like our fault, and an
                evaluator has no way to tell the difference. */}
            {target?.unstable && (
              <div className="hint">
                The source keeps dropping: the relay has restarted several times
                in the last minute. This is the upstream feed, not the platform —
                the camera&apos;s health history shows the same pattern.
              </div>
            )}
            {phase === 'reconnecting' && (
              <div className="hint">attempt {attempt + 1} of {MAX_RECONNECTS + 1}</div>
            )}
          </div>
        )}
      </div>
      <div className="live-meta">
        <span className={`live-badge live-${phase}`}>{PHASE_TEXT[phase]}</span>
        {ttfbMs != null && (
          <span data-testid="live-ttfb" data-ttfb-ms={ttfbMs}>
            first frame in <b>{(ttfbMs / 1000).toFixed(2)} s</b>
          </span>
        )}
        {target?.unstable && <span className="live-badge live-error">upstream unstable</span>}
        {target && (
          <span className="muted">
            {target.adapter} → {target.protocol}
            {target.relayed ? ' · pulled on demand' : ' · direct'}
            {/* Worth saying out loud: this camera speaks a codec no browser
                can play, and the source is still untouched for analytics. */}
            {target.transcoded && ' · re-encoded for the browser'}
          </span>
        )}
      </div>
    </div>
  )
}
