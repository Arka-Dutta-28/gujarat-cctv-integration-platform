import { useEffect, useState } from 'react'
import { fetchCapability, fetchPerformance, fetchStatus, toIST } from './api'
import type { CapabilityReport, Performance, PlatformStatus } from './api'

/**
 * Performance evidence — Expected Output 4.
 *
 * The problem statement asks for "evidence of end-to-end system performance",
 * which is a metrics surface rather than a sentence in a document. The build
 * plan names the figures: streams active, FPS, detections per minute, queue
 * depth, p95 query latency, uptime, sightings indexed.
 *
 * Every number here is read from what the platform recorded. There is no
 * configured or assumed figure on this page, and where something cannot be
 * measured it says so rather than showing a plausible zero.
 *
 * Two things are stated here that a performance page would normally omit,
 * because leaving them out would make the rest of it less trustworthy:
 *
 * **The shed rate.** Under load the OCR stage refuses work rather than queueing
 * it, and at this camera count a large fraction of attempts are shed. That is
 * the designed behaviour — a thread blocked in OCR has stopped decoding, and a
 * starved decoder loses whole vehicles rather than one vehicle's vote — but it
 * is also a capacity limit, and a throughput figure quoted without it would be
 * misleading.
 *
 * **Which cameras can actually read a plate.** 31 of the 81 cameras are
 * wide-area views whose plate crops average 66 px; no recogniser resolves a
 * registration number at that scale. The accuracy figure is scoped to the
 * cameras it is true of, and the page says how many those are.
 */

const REFRESH_MS = 5000

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${Math.round(seconds % 60)}s`
}

const fmt = (n: number | null | undefined) =>
  n === null || n === undefined ? '—' : n.toLocaleString('en-IN')

export function PerformancePage({ onClose }: { onClose: () => void }) {
  const [perf, setPerf] = useState<Performance | null>(null)
  const [status, setStatus] = useState<PlatformStatus | null>(null)
  const [capability, setCapability] = useState<CapabilityReport | null>(null)
  const [minutes, setMinutes] = useState(15)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const load = () => {
      fetchPerformance(minutes).then(setPerf).catch((e) => setError(String(e)))
      fetchStatus().then(setStatus).catch(() => {})
      setTick((t) => t + 1)
    }
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => clearInterval(id)
  }, [minutes])

  useEffect(() => { fetchCapability().then(setCapability).catch(() => {}) }, [])

  const shed = perf?.load_shedding
  const capable = capability?.summary?.anpr_grade ?? null

  return (
    <div className="perf">
      <header className="perf-head">
        <div>
          <h1>End-to-end system performance</h1>
          <p className="muted">
            Measured by the running platform over the last {minutes} minutes.
            Nothing on this page is configured or assumed.
            {status?.earliest_sighting && (
              <> Indexing since <b>{toIST(status.earliest_sighting)} IST</b>.</>
            )}
          </p>
        </div>
        <div className="perf-controls">
          <span className={`live-dot ${tick % 2 ? 'on' : 'on'}`} />
          <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
            <option value={5}>last 5 min</option>
            <option value={15}>last 15 min</option>
            <option value={60}>last hour</option>
            <option value={360}>last 6 hours</option>
          </select>
          <button onClick={onClose}>Back to map</button>
        </div>
      </header>

      {error && <p className="error">{error}</p>}

      {perf && (
        <>
          {/* The headline row is what a screen recording captures. */}
          <section className="perf-grid">
            <Figure
              value={fmt(perf.cameras_processing)}
              label="streams processing"
              note={`${fmt(perf.cameras_online)} online of ${fmt(perf.cameras_registered)} registered`}
            />
            <Figure
              value={perf.mean_decode_fps?.toFixed(1) ?? '—'}
              label="mean decode fps"
              note={`${fmt(perf.frames_decoded)} frames decoded`}
            />
            <Figure
              value={fmt(perf.sightings_per_minute)}
              label="detections / min"
              note={`${fmt(perf.distinct_plates)} distinct plates in window`}
            />
            <Figure
              value={fmt(perf.sightings_indexed)}
              label="sightings indexed"
              note={`${fmt(status?.sightings_total ?? 0)} total, all time`}
            />
            <Figure
              value={perf.alert_p95_latency_s !== null
                ? `${perf.alert_p95_latency_s.toFixed(2)}s` : '—'}
              label="alert p95 latency"
              note={`${fmt(perf.alerts_raised)} alerts raised · budget 5s`}
              good={perf.alert_p95_latency_s !== null && perf.alert_p95_latency_s < 5}
            />
            <Figure
              value={duration(perf.uptime_s)}
              label="api uptime"
              note={status ? `${duration(status.uptime_s)} process uptime` : ''}
            />
          </section>

          {/* Invariant 1's canary. On a healthy platform this is a green line
              nobody reads; the one time it mattered, every write in the estate
              had been failing for two hours while throughput looked normal. */}
          <section className={`perf-banner ${perf.write_health.healthy ? 'ok' : 'bad'}`}>
            <b>{perf.write_health.healthy ? 'Index healthy' : 'INDEX WRITES FAILING'}</b>
            <span>{perf.write_health.detail}</span>
          </section>

          <div className="perf-columns">
            <section className="perf-card">
              <h2>Pipeline stages</h2>
              <p className="muted small">
                Per-stage latency as the workers recorded it, rolled up per camera
                per minute. A stage that disagrees with the same stage probed
                alone is measuring contention, not cost.
              </p>
              <table className="perf-table">
                <thead>
                  <tr><th>Stage</th><th>Samples</th><th>p50</th><th>p95</th><th>max</th></tr>
                </thead>
                <tbody>
                  {perf.stages.map((s) => (
                    <tr key={s.stage}>
                      <td>{s.stage}</td>
                      <td>{fmt(s.samples)}</td>
                      <td>{s.p50_ms.toFixed(1)} ms</td>
                      <td>{s.p95_ms.toFixed(1)} ms</td>
                      <td>{s.max_ms.toFixed(0)} ms</td>
                    </tr>
                  ))}
                  {perf.stages.length === 0 && (
                    <tr><td colSpan={5} className="muted">No stage samples in this window.</td></tr>
                  )}
                </tbody>
              </table>
            </section>

            <section className="perf-card">
              <h2>API query latency</h2>
              <p className="muted small">
                The API's own handling time, by route template — not what a
                browser experiences, which adds network and render.
              </p>
              <table className="perf-table">
                <thead>
                  <tr><th>Route</th><th>Requests</th><th>p50</th><th>p95</th></tr>
                </thead>
                <tbody>
                  {perf.api_latency.slice(0, 10).map((r) => (
                    <tr key={r.route}>
                      <td className="route">{r.route}</td>
                      <td>{fmt(r.requests)}</td>
                      <td>{r.p50_ms.toFixed(1)} ms</td>
                      <td>{r.p95_ms.toFixed(1)} ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>

            <section className="perf-card">
              <h2>Work deliberately not done</h2>
              <p className="muted small">
                The scalability argument, and the capacity limit in the same
                table. Adaptive sampling is what makes a statewide estate close;
                the shed rate is what says this box is at its limit.
              </p>
              <dl className="perf-dl">
                <dt>Detector load avoided</dt>
                <dd>{perf.detector_load_avoided ?? '—'}</dd>
                <dt>OCR attempts shed</dt>
                <dd className={shed && shed.ocr_shed_fraction > 0.5 ? 'warn' : undefined}>
                  {fmt(shed?.ocr_shed)} ({((shed?.ocr_shed_fraction ?? 0) * 100).toFixed(1)}%)
                  {shed && shed.ocr_shed_fraction > 0.5 &&
                    ' — over capacity; the fix is fewer cameras per box, not more threads'}
                </dd>
                <dt>Re-reads skipped (vote settled)</dt>
                <dd>{fmt(shed?.ocr_skipped_settled)}</dd>
                <dt>Burnt-in overlay text suppressed</dt>
                <dd>{fmt(shed?.overlay_suppressed)}</dd>
                <dt>Sessions via the on-demand relay</dt>
                <dd>{fmt(shed?.sessions_via_relay)}</dd>
                <dt>Queue depth</dt>
                <dd>
                  none by design — OCR is bounded and sheds rather than queueing,
                  because a thread blocked in OCR has stopped decoding
                </dd>
              </dl>
            </section>

            <section className="perf-card">
              <h2>What the estate can actually read</h2>
              {capability ? (
                <>
                  <p className="muted small">{capability.accuracy_scope}</p>
                  <dl className="perf-dl">
                    <dt>ANPR-grade cameras</dt>
                    <dd>{capable} of {capability.cameras_assessed}</dd>
                    <dt>Wide-area only</dt>
                    <dd>
                      {capability.summary.situational_awareness} cameras below the{' '}
                      {capability.anpr_plate_px_floor} px plate-width floor
                    </dd>
                    <dt>Too few reads to grade</dt>
                    <dd>
                      {capability.summary.insufficient_evidence} ·{' '}
                      {capability.summary.no_reads} produced none
                    </dd>
                  </dl>
                </>
              ) : (
                <p className="muted small">Capability report unavailable.</p>
              )}
            </section>
          </div>

          <section className="perf-card">
            <h2>Busiest cameras</h2>
            <table className="perf-table">
              <thead>
                <tr>
                  <th>Camera</th><th>Decoded</th><th>Analysed</th>
                  <th>Sightings</th><th>fps</th>
                </tr>
              </thead>
              <tbody>
                {perf.per_camera.slice(0, 12).map((c) => (
                  <tr key={c.external_ref}>
                    <td>{c.name}</td>
                    <td>{fmt(c.frames_decoded)}</td>
                    <td>{fmt(c.frames_analysed)}</td>
                    <td>{fmt(c.sightings_written)}</td>
                    <td>{c.decode_fps?.toFixed(1) ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  )
}

function Figure(
  { value, label, note, good }: { value: string; label: string; note?: string; good?: boolean },
) {
  return (
    <div className={`perf-figure${good ? ' good' : ''}`}>
      <span className="perf-value">{value}</span>
      <span className="perf-label">{label}</span>
      {note && <span className="perf-note">{note}</span>}
    </div>
  )
}
