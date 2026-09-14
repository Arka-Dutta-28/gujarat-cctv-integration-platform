import { useCallback, useEffect, useState } from 'react'
import { clearBoost, fetchBoosts, startBoost } from './api'
import type { BoostTarget, OcrBoost } from './api'

const REFRESH_MS = 5000

const STATUS_TEXT: Record<OcrBoost['status'], string> = {
  pending: 'waiting for a worker',
  running: 'running',
  failed: 'could not start',
  expired: 'ended',
  cleared: 'stopped',
}

function until(iso: string): string {
  const mins = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 60000))
  return mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m left` : `${mins}m left`
}

interface Props {
  /** A place (the trace's last sighting) or one camera (the detail panel). */
  mode: 'near' | 'camera'
  cameraId?: string
  lat?: number
  lon?: number
  canWrite: boolean
  caseRef?: string
}

/**
 * Ask for the stronger reader (PaddleOCR-VL) on the cameras that matter now.
 *
 * Nothing here decides anything: the operator picks the cameras, the API caps
 * and audits the request, and each ANPR worker reports whether it could run it.
 * The status shown is the worker's answer, refreshed every few seconds, so a
 * boost that cannot run on a worker without a GPU says so instead of looking
 * like it helped.
 */
export function OcrBoostControl({ mode, cameraId, lat, lon, canWrite, caseRef }: Props) {
  const [radiusKm, setRadiusKm] = useState(5)
  const [minutes, setMinutes] = useState(60)
  const [cameraIds, setCameraIds] = useState<string[]>(cameraId ? [cameraId] : [])
  const [boosts, setBoosts] = useState<OcrBoost[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A different camera or place starts clean.
  useEffect(() => { setCameraIds(cameraId ? [cameraId] : []); setError(null) }, [cameraId, lat, lon])

  const refresh = useCallback(async () => {
    if (cameraIds.length === 0) { setBoosts([]); return }
    try {
      const all = await fetchBoosts()
      setBoosts(all.filter((b) => cameraIds.includes(b.camera_id)))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [cameraIds])

  useEffect(() => {
    void refresh()
    if (cameraIds.length === 0) return
    const timer = window.setInterval(() => void refresh(), REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refresh, cameraIds])

  async function start() {
    setBusy(true)
    setError(null)
    try {
      const target: BoostTarget = mode === 'camera'
        ? { camera_ids: [cameraId!] }
        : { near: { lat: lat!, lon: lon!, radius_m: radiusKm * 1000 } }
      const started = await startBoost(target, {
        minutes, case_ref: caseRef || undefined,
        reason: mode === 'near' ? 'cameras around the last sighting' : 'single camera',
      })
      setCameraIds(started.map((b) => b.camera_id))
      setBoosts(started)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function stopAll() {
    setBusy(true)
    try {
      await Promise.all(boosts.map((b) => clearBoost(b.camera_id)))
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const readOnly = !canWrite ? 'Operators only — this account is read-only' : undefined
  const label = mode === 'near' ? 'Boost cameras near last sighting' : 'Boost this camera'

  return (
    <div className="boost" data-no-drag>
      <div className="boost-row">
        {mode === 'near' && (
          <select value={radiusKm} onChange={(e) => setRadiusKm(Number(e.target.value))}
                  aria-label="Radius" title="Cameras within this distance of the last sighting">
            <option value={2}>2 km</option>
            <option value={5}>5 km</option>
            <option value={10}>10 km</option>
          </select>
        )}
        <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}
                aria-label="Duration" title="The boost ends on its own after this">
          <option value={30}>30 min</option>
          <option value={60}>1 hour</option>
          <option value={180}>3 hours</option>
        </select>
        <button className="export" onClick={() => void start()}
                disabled={busy || !canWrite}
                title={readOnly ?? 'Read these cameras with PaddleOCR-VL, the strongest reader tested. Needs a GPU worker.'}>
          {busy ? '…' : label}
        </button>
        {boosts.some((b) => b.status === 'pending' || b.status === 'running' || b.status === 'failed') && (
          <button className="link" onClick={() => void stopAll()} disabled={busy || !canWrite}
                  title={readOnly ?? 'Back to the default reader'}>
            Stop
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      {boosts.length > 0 && (
        <ul className="boost-list">
          {boosts.map((b) => (
            <li key={b.id}>
              <span>{b.camera_name}</span>{' '}
              <span className={`boost-status boost-${b.status}`}>{STATUS_TEXT[b.status]}</span>
              {(b.status === 'running' || b.status === 'pending') && (
                <span className="muted small"> · {until(b.expires_at)}</span>
              )}
              {b.apply_error && <div className="muted small">{b.apply_error}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
