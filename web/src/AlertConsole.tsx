import { useCallback, useEffect, useRef, useState } from 'react'
import { useDraggable } from './useDraggable'
import {
  addToWatchlist, alertSocketUrl, cropUrl, fetchAlertStats, fetchAlerts, setAlertStatus,
} from './api'
import { AuthedImage } from './AuthedImage'
import type { Alert, AlertStats } from './api'

interface Props {
  onFocus: (lat: number, lon: number) => void
}

const IST = 'Asia/Kolkata'

function ist(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-IN', {
    timeZone: IST, hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** Tier is evidence quality, so it is shown as a word, never as a colour alone. */
const TIER_LABEL: Record<string, string> = {
  confirmed: 'Confirmed',
  probable: 'Probable',
  possible: 'Possible',
  // Spelled out because it is the one tier where the word "match" would
  // mislead: no plate was read at all. An operator reading "Appearance" knows
  // immediately that they are looking at a vehicle that resembles the wanted
  // one, not at a vehicle identified as it.
  attribute: 'Appearance only',
}

/**
 * Live alert console — the M5 operator surface.
 *
 * Alerts arrive over a WebSocket as they are raised and are also loaded once
 * over REST, because a console that starts empty until the next vehicle passes
 * is indistinguishable from one that is broken.
 *
 * Two decisions worth reading:
 *
 * The **tier** is rendered as a word next to the plate, not encoded in a colour.
 * `probable` means "this is one character from a wanted vehicle, check the
 * crop"; an operator acting on it is making a different decision from one
 * acting on `confirmed`, and a colour cannot carry that. `attribute` is
 * labelled "Appearance only" for the same reason, in stronger terms: no plate
 * was read, and the word "match" on its own would overstate what happened.
 *
 * The **crop is the headline**, not the plate string. The image is what an
 * officer stopping a vehicle is actually judging, and showing the text alone
 * would present an OCR read as a fact rather than as evidence.
 */
export function AlertConsole({ onFocus }: Props) {
  const drag = useDraggable<HTMLElement>('alerts')
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [stats, setStats] = useState<AlertStats | null>(null)
  const [live, setLive] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [plate, setPlate] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const socket = useRef<WebSocket | null>(null)

  const merge = useCallback((incoming: Alert[]) => {
    setAlerts((current) => {
      const seen = new Set(current.map((a) => a.id))
      const fresh = incoming.filter((a) => !seen.has(a.id))
      if (!fresh.length) return current
      return [...fresh, ...current].slice(0, 100)
    })
  }, [])

  useEffect(() => {
    fetchAlerts(50).then(setAlerts).catch(() => {})
    const load = () => fetchAlertStats().then(setStats).catch(() => {})
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    let closed = false
    let retry: number | undefined

    const connect = () => {
      if (closed) return
      const ws = new WebSocket(alertSocketUrl())
      socket.current = ws
      ws.onopen = () => setLive(true)
      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data)
          if (payload.type === 'alerts' && Array.isArray(payload.alerts)) {
            merge(payload.alerts)
            setFlash(payload.alerts[0]?.id ?? null)
            setTimeout(() => setFlash(null), 4000)
          }
        } catch { /* a malformed frame must not take the console down */ }
      }
      ws.onclose = () => {
        setLive(false)
        // An operations room leaves this open for a shift; a dropped socket
        // must recover on its own rather than needing a reload nobody knows to
        // do. REST polling of the stats keeps the panel honest meanwhile.
        if (!closed) retry = window.setTimeout(connect, 3000)
      }
      ws.onerror = () => ws.close()
    }
    connect()
    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      socket.current?.close()
    }
  }, [merge])

  async function arm(e: React.FormEvent) {
    e.preventDefault()
    const target = plate.trim()
    if (!target) return
    setBusy(true)
    setError(null)
    try {
      const entry = await addToWatchlist({ plate: target, category: 'wanted', severity: 4 })
      setPlate('')
      setFlash(entry.id)
      setStats((s) => (s ? { ...s, watchlist_active: s.watchlist_active + 1 } : s))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function resolve(alert: Alert, status: string) {
    const updated = await setAlertStatus(alert.id, status)
    setAlerts((current) => current.map((a) => (a.id === updated.id ? updated : a)))
  }

  const unacknowledged = alerts.filter((a) => a.status === 'new').length

  return (
    <section
      className={`panel panel-alerts draggable${collapsed ? ' collapsed' : ''}`}
      {...drag.panelProps}
    >
      <div className="title-row drag-handle" {...drag.handleProps}>
        <span className="title">
          Live alerts
          {unacknowledged > 0 && <span className="badge-count">{unacknowledged}</span>}
        </span>
        <span className="spacer" />
        <span className={`live-dot ${live ? 'on' : 'off'}`} title={live ? 'Streaming' : 'Reconnecting…'} />
        <button
          className="collapse-toggle"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? 'Expand alert console' : 'Collapse alert console'}
        >
          {collapsed ? '▴' : '▾'}
        </button>
      </div>

      <div className="alerts-body" data-no-drag>
        <form className="watch-form" onSubmit={arm}>
          <input
            value={plate}
            onChange={(e) => setPlate(e.target.value.toUpperCase())}
            placeholder="Add plate to watchlist"
            aria-label="Plate to watch"
            spellCheck={false}
          />
          <button className="primary" type="submit" disabled={busy || !plate.trim()}>
            {busy ? 'Arming…' : 'Watch'}
          </button>
        </form>
        {error && <p className="error">{error}</p>}

        {stats && (
          <div className="alert-stats">
            <span>{stats.watchlist_active} watched</span>
            <span>{stats.alerts} alerts / {stats.window_hours}h</span>
            {stats.p95_latency_s !== null && (
              <span title="Sighting written to alert raised. Budget is 5 s.">
                p95 {stats.p95_latency_s.toFixed(2)}s
              </span>
            )}
          </div>
        )}

        {alerts.length === 0 && (
          <p className="muted small">
            No alerts yet. Add a plate above; the next sighting of it raises one.
          </p>
        )}

        <ul className="alert-list">
          {alerts.map((a) => (
            <li
              key={a.id}
              className={
                `alert tier-${a.tier} status-${a.status}` +
                (flash === a.id ? ' flash' : '')
              }
              onClick={() => a.lat != null && a.lon != null && onFocus(a.lat, a.lon)}
            >
              {a.thumbnail_url ? (
                <AuthedImage
                  className="alert-crop"
                  src={cropUrl(a.thumbnail_url)}
                  alt={`Crop for ${a.plate_read}`}
                />
              ) : (
                <div className="alert-crop empty" title="No crop stored for this sighting">—</div>
              )}
              <div className="alert-body">
                <div className="alert-head">
                  <strong>{a.plate}</strong>
                  <span className={`tier tier-${a.tier}`}>{TIER_LABEL[a.tier] ?? a.tier}</span>
                  <span className="muted small">{a.category} · sev {a.severity}</span>
                </div>
                <div className="muted small">
                  {a.camera_name ?? a.camera_id}
                  {a.district ? ` · ${a.district}` : ''} · {ist(a.sighting_ts)} IST
                </div>
                <div className="muted small">
                  {/* What the camera read, next to what is wanted. When they
                      differ this is a probable/possible match, and seeing both
                      is how an operator judges it. */}
                  read <code>{a.plate_read}</code> at {(a.confidence * 100).toFixed(0)}%
                  {' · '}alerted in {a.detection_latency_s.toFixed(2)}s
                  {a.case_ref ? ` · ${a.case_ref}` : ''}
                </div>
                {a.status === 'new' ? (
                  <div className="alert-actions">
                    <button onClick={(e) => { e.stopPropagation(); void resolve(a, 'acknowledged') }}>
                      Acknowledge
                    </button>
                    <button onClick={(e) => { e.stopPropagation(); void resolve(a, 'false_positive') }}>
                      Not this vehicle
                    </button>
                  </div>
                ) : (
                  <div className="muted small">
                    {a.status.replace(/_/g, ' ')}
                    {a.acknowledged_by ? ` by ${a.acknowledged_by}` : ''}
                  </div>
                )}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
