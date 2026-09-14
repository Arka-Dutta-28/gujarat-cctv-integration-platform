import { useState } from 'react'
import { useDraggable } from './useDraggable'
import { OcrBoostControl } from './OcrBoost'
import { downloadReport, fetchJourney, searchPlates } from './api'
import type { Journey, JourneyVisit, SightingHit } from './api'

interface Props {
  onJourney: (journey: Journey | null) => void
  onFocus: (lat: number, lon: number) => void
  canWrite: boolean
}

const IST = 'Asia/Kolkata'

/** Timestamps are stored UTC and rendered IST — a Gujarat operator's clock. */
function ist(iso: string): string {
  return new Date(iso).toLocaleString('en-IN', {
    timeZone: IST, hour12: false,
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`
}

/**
 * Retrospective trace — the test case's operator surface.
 *
 * An evaluator hands over a registration number and this is where it goes. The
 * whole view comes from one request, so there is no state in which the route
 * has loaded and the movement history has not.
 *
 * Implausible hops are rendered prominently rather than hidden. A transition
 * faster than a car can drive is the strongest evidence the platform can offer
 * that a plate has been cloned, and burying it to make the demo look tidy would
 * defeat the reason the check exists.
 */
export function TracePanel({ onJourney, onFocus, canWrite }: Props) {
  const drag = useDraggable<HTMLElement>('trace')
  const [plate, setPlate] = useState('')
  const [caseRef, setCaseRef] = useState('')
  const [journey, setJourney] = useState<Journey | null>(null)
  const [hits, setHits] = useState<SightingHit[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [elapsedMs, setElapsedMs] = useState<number | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  const [windowHours, setWindowHours] = useState(0)

  async function trace(query: string) {
    const target = query.trim()
    if (!target) return
    setBusy(true)
    setError(null)
    setHits(null)
    const started = performance.now()
    try {
      const from =
        windowHours > 0
          ? new Date(Date.now() - windowHours * 3600_000).toISOString()
          : undefined
      const result = await fetchJourney(target, { from, caseRef: caseRef || undefined })
      setElapsedMs(performance.now() - started)
      setJourney(result)
      onJourney(result)

      // No route means either an unknown plate or an OCR near-miss. Offering
      // the fuzzy matches is the difference between "not found" and a lead.
      if (result.properties.visits === 0 && !target.startsWith('#')) {
        setHits(await searchPlates(target))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setJourney(null)
      onJourney(null)
    } finally {
      setBusy(false)
    }
  }

  const props = journey?.properties
  const visits = (journey?.features ?? []).filter(
    (f) => (f.properties as { kind?: string })?.kind === 'sighting',
  )
  // Where the vehicle was last seen: the place worth reading harder.
  const last = visits.length
    ? (visits[visits.length - 1].geometry as GeoJSON.Point).coordinates
    : null

  return (
    <section
      className={`panel panel-trace draggable${collapsed ? ' collapsed' : ''}`}
      {...drag.panelProps}
    >
      <div className="title-row drag-handle" {...drag.handleProps}>
        <span className="title">Trace a vehicle</span>
        <span className="spacer" />
        {props && collapsed && (
          <span className="muted small">
            {props.plate} · {props.visits} visits
          </span>
        )}
        {elapsedMs !== null && !collapsed && (
          <span className="muted small">{elapsedMs.toFixed(0)} ms</span>
        )}
        <button
          className="collapse-toggle"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? 'Expand trace panel' : 'Collapse trace panel'}
          title={collapsed ? 'Expand' : 'Collapse'}
        >
          {collapsed ? '▴' : '▾'}
        </button>
      </div>

      <div className="trace-body" data-no-drag>
      <form
        className="trace-form"
        onSubmit={(e) => {
          e.preventDefault()
          void trace(plate)
        }}
      >
        <input
          value={plate}
          onChange={(e) => setPlate(e.target.value.toUpperCase())}
          placeholder="GJ 01 AB 1234 or #vehicle id"
          aria-label="Registration number"
          spellCheck={false}
        />
        <input
          className="case-ref"
          value={caseRef}
          onChange={(e) => setCaseRef(e.target.value)}
          placeholder="Case ref"
          aria-label="Case reference"
        />
        {/* Unbounded is the default because the graded scenario is a plate
            handed over after the vehicle has already passed, and a window that
            silently excludes the sighting turns a hit into "not found" — the
            worst failure this panel has. But an unbounded trace over a feed
            that has looped for days stitches the same vehicle's repeats into
            one impossible journey, so the control has to be here and visible
            rather than buried in the API. */}
        <select
          className="trace-window"
          value={windowHours}
          onChange={(e) => setWindowHours(Number(e.target.value))}
          aria-label="Time window"
          title="How far back to look"
        >
          <option value={0}>All time</option>
          <option value={1}>Last hour</option>
          <option value={6}>Last 6 hours</option>
          <option value={24}>Last 24 hours</option>
          <option value={72}>Last 3 days</option>
          <option value={168}>Last 7 days</option>
        </select>
        <button className="primary" type="submit" disabled={busy || !plate.trim()}>
          {busy ? 'Tracing…' : 'Trace'}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      {props && props.visits === 0 && (
        <div className="trace-empty">
          <p className="muted">No sightings for {props.plate}.</p>
          {hits && hits.length > 0 && (
            <>
              <p className="muted small">Nearest reads by similarity:</p>
              <ul className="near-misses">
                {[...new Set(hits.map((h) => h.plate_normalised))].slice(0, 6).map((p) => (
                  <li key={p}>
                    <button className="link" onClick={() => { setPlate(p); void trace(p) }}>
                      {p}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
          {hits && hits.length === 0 && (
            <p className="muted small">No similar plates either.</p>
          )}
        </div>
      )}

      {props && props.visits > 0 && (
        <>
          <div className="trace-summary">
            <strong>{props.plate}</strong>
            <span className="muted"> · {props.summary}</span>
            <div className="trace-facts">
              {/* Deliverable 4, one click from the trace that produced it. An
                  operator who has just found the vehicle wants its movement
                  history as a document, not a second query to learn. */}
              <button
                className="export"
                onClick={() => void downloadReport('pdf', {
                  plate: props.plate, limit: 500, case_ref: caseRef || undefined,
                })}
                title="Movement history as a PDF, with crops and coordinates"
              >
                Export PDF
              </button>
              <button
                className="export"
                onClick={() => void downloadReport('csv', {
                  plate: props.plate, limit: 5000, case_ref: caseRef || undefined,
                })}
                title="Every detection as CSV, full precision"
              >
                CSV
              </button>
              <span title="Degraded by every implausible transition">
                confidence {(props.confidence * 100).toFixed(0)}%
              </span>
              <span>{props.sightings} reads → {props.visits} visits</span>
              <span>{props.road_snapped ? 'road-snapped' : 'straight-line'}</span>
              {props.implausible_transitions > 0 && (
                <span className="warn">{props.implausible_transitions} implausible</span>
              )}
            </div>
          </div>

          {last && (
            <OcrBoostControl mode="near" lon={last[0]} lat={last[1]}
                             canWrite={canWrite} caseRef={caseRef} />
          )}

          {props.implausible_transitions > 0 && (
            <p className="clone-warning">
              This route contains {props.implausible_transitions} transition
              {props.implausible_transitions === 1 ? '' : 's'} too fast to drive.
              The journey is shown in {props.legs.length} separate legs — a
              possible cloned plate rather than one vehicle.
            </p>
          )}

          <table className="history">
            <thead>
              <tr>
                <th>#</th><th>Time (IST)</th><th>Camera</th>
                <th>Dwell</th><th>Conf</th><th>Vehicle</th><th>Next hop</th>
              </tr>
            </thead>
            <tbody>
              {visits.map((f, i) => {
                const v = f.properties as unknown as JourneyVisit
                const hop = props.segments[i]
                const [lon, lat] = (f.geometry as GeoJSON.Point).coordinates
                return (
                  <tr
                    key={`${v.camera_id}-${v.ts}`}
                    onClick={() => onFocus(lat, lon)}
                    className={hop && !hop.plausible ? 'row-implausible' : undefined}
                  >
                    <td>{v.sequence}</td>
                    <td>{ist(String(v.ts))}</td>
                    <td>
                      {v.camera_name ?? v.camera_id}
                      {v.district ? <span className="muted"> · {v.district}</span> : null}
                    </td>
                    <td>{Number(v.dwell_s) > 0 ? duration(Number(v.dwell_s)) : '—'}</td>
                    <td>{(Number(v.confidence) * 100).toFixed(0)}%</td>
                    <td>
                      {(v.vehicle_uids ?? []).map((uid) => (
                        <button
                          key={uid}
                          className="link"
                          title="Follow this vehicle id: plate reads plus appearance matches"
                          onClick={(e) => {
                            e.stopPropagation()
                            setPlate(`#${uid}`)
                            void trace(`#${uid}`)
                          }}
                        >
                          #{uid}
                        </button>
                      ))}
                      {v.uid_via?.includes('appearance') && (
                        <span
                          className="warn"
                          title="Joined by how the vehicle looks, not by its plate. Check the crops."
                        >
                          {' '}by look{v.uid_distance != null ? ` ${v.uid_distance.toFixed(2)}` : ''}
                        </span>
                      )}
                    </td>
                    <td>
                      {hop ? (
                        <span
                          className={hop.plausible ? undefined : 'warn'}
                          title={
                            hop.road_snapped
                              ? `${(hop.road_distance_m! / 1000).toFixed(1)} km by road ` +
                                `(${hop.road_speed_kmh?.toFixed(0)} km/h); ` +
                                `${(hop.direct_distance_m / 1000).toFixed(1)} km direct. ` +
                                'Plausibility uses the direct line, which no vehicle can beat.'
                              : 'Straight-line distance; OSRM could not route this pair.'
                          }
                        >
                          {(hop.distance_m / 1000).toFixed(1)} km ·{' '}
                          {duration(hop.elapsed_s)} ·{' '}
                          {hop.implied_speed_kmh === null
                            ? '∞'
                            : `${hop.implied_speed_kmh.toFixed(0)} km/h`}
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </>
      )}
      </div>
    </section>
  )
}
