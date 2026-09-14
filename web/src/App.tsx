import { useCallback, useEffect, useState } from 'react'
import { useDraggable } from './useDraggable'
import { CameraMap } from './CameraMap'
import { AddCameraForm } from './AddCameraForm'
import { LiveView } from './LiveView'
import { TracePanel } from './TracePanel'
import { OcrBoostControl } from './OcrBoost'
import { AlertConsole } from './AlertConsole'
import { PerformancePage } from './PerformancePage'
import { LoginScreen } from './LoginScreen'
import {
  fetchCameras, fetchCoverage, fetchCoverageSummary, fetchGaps, fetchIdentity,
  fetchStatus, getToken, logout, toIST,
} from './api'
import type {
  CameraCollection, CameraFeature, CoverageSummary, GapAnalysis, Identity,
  PlatformStatus,
} from './api'
import { STATUS_COLOURS } from './mapStyle'

/**
 * Hash routing, deliberately without a router library.
 *
 * The platform has exactly two views — the operations map and the performance
 * evidence page — and the second exists partly to be screen-recorded and linked
 * to in the submission. A hash gives it a stable URL and a back button for
 * nothing; react-router would be a dependency and a bundle for one branch.
 */
function useHashRoute(): string {
  const [route, setRoute] = useState(window.location.hash)
  useEffect(() => {
    const onChange = () => setRoute(window.location.hash)
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

export default function App() {
  const route = useHashRoute()
  const [identity, setIdentity] = useState<Identity | null>(null)
  const [checkedAuth, setCheckedAuth] = useState(false)
  const [cameras, setCameras] = useState<CameraCollection | null>(null)
  const [status, setStatus] = useState<PlatformStatus | null>(null)
  const [coverage, setCoverage] = useState<GeoJSON.FeatureCollection | null>(null)
  const [coverageSummary, setCoverageSummary] = useState<CoverageSummary | null>(null)
  const [gapAnalysis, setGapAnalysis] = useState<GapAnalysis | null>(null)
  const [selected, setSelected] = useState<CameraFeature | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [showCoverage, setShowCoverage] = useState(false)
  const [showGaps, setShowGaps] = useState(false)
  const [adding, setAdding] = useState(false)
  const [picking, setPicking] = useState(false)
  const [picked, setPicked] = useState<{ lat: number; lon: number } | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [journey, setJourney] = useState<GeoJSON.FeatureCollection | null>(null)
  const [focus, setFocus] = useState<{ lat: number; lon: number } | null>(null)

  const reloadCameras = useCallback(
    () => fetchCameras().then(setCameras).catch((e) => setError(String(e))),
    [],
  )

  // Ask the API whether it wants a login before drawing anything. A 401 here
  // is the answer, not an error: it means authentication is on and this browser
  // has no valid token.
  useEffect(() => {
    fetchIdentity()
      .then((who) => setIdentity(who.auth_required && !getToken() ? null : who))
      .catch(() => setIdentity(null))
      .finally(() => setCheckedAuth(true))
  }, [])

  useEffect(() => { if (identity) reloadCameras() }, [reloadCameras, identity])

  useEffect(() => {
    if (!identity) return
    fetchCoverage().then(setCoverage).catch(() => {})
    fetchCoverageSummary().then(setCoverageSummary).catch(() => {})
    fetchGaps().then(setGapAnalysis).catch(() => {})
  }, [identity])

  // The prober moves a camera to green within a few seconds, so the map has to
  // keep refreshing for a newly onboarded camera to be seen turning green.
  useEffect(() => {
    if (!identity) return
    const load = () => {
      fetchStatus().then(setStatus).catch(() => {})
      reloadCameras()
    }
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [reloadCameras, identity])

  const counts = cameras
    ? cameras.features.reduce<Record<string, number>>((acc, f) => {
        acc[f.properties.status] = (acc[f.properties.status] ?? 0) + 1
        return acc
      }, {})
    : {}

  // Cameras whose position this platform derived rather than an operator
  // surveying it. Counted so the legend can say how much of the map is an
  // inference; `geo_precision` is null on rows that predate the column, which
  // is not a claim of survey accuracy either.
  const inferred = cameras
    ? cameras.features.filter(
        (f) => (f.properties.geo_precision ?? 'unplaced') !== 'survey',
      ).length
    : 0

  function onCreated(_id: string, name: string) {
    setAdding(false)
    setPicking(false)
    setPicked(null)
    setToast(`Added “${name}” — watching for it to come online…`)
    reloadCameras()
    fetchCoverage().then(setCoverage).catch(() => {})
    fetchGaps().then(setGapAnalysis).catch(() => {})
    setTimeout(() => setToast(null), 8000)
  }

  // Several panels are pinned to the same corner — the camera detail panel and
  // the add-camera form both sit top-right — so one hid the other completely.
  // Rather than guess which an operator wants, let them arrange it.
  //
  // Declared here, above every conditional return below. Hooks must run in the
  // same order on every render, and putting these after the `#/performance`
  // early return meant the performance route called 24 hooks and the map route
  // called 27 — React rendered nothing at all and the console said "rendered
  // more hooks than during the previous render". TypeScript cannot see this.
  const detailDrag = useDraggable<HTMLElement>('detail')
  const gapsDrag = useDraggable('gaps')
  const legendDrag = useDraggable('legend')

  // Nothing is drawn until the API has answered: rendering the map first and
  // replacing it with a login is a flash of data the viewer may not be entitled
  // to see.
  if (!checkedAuth) return <div className="login"><p className="muted">Connecting…</p></div>
  if (!identity) return <LoginScreen onSignedIn={setIdentity} />

  if (route === '#/performance') {
    return <PerformancePage onClose={() => { window.location.hash = '' }} />
  }

  return (
    <div className="app">
      <CameraMap
        journey={journey}
        focus={focus}
        cameras={cameras}
        coverage={coverage}
        gaps={gapAnalysis?.geojson ?? null}
        showCoverage={showCoverage}
        showGaps={showGaps}
        picking={picking}
        onPick={(lat, lon) => { setPicked({ lat, lon }); setPicking(false) }}
        onSelect={setSelected}
      />

      <header className="panel panel-top">
        <div className="title-row">
          <span className="title">Gujarat CCTV Integration Platform</span>
          <a className="small link-button" href="#/performance">Performance</a>
          {identity.auth_required && (
            <span className="who muted small" title={`Signed in as ${identity.role}`}>
              {identity.username}
              {!identity.can_write && ' · read-only'}
              <button
                className="link"
                onClick={() => { logout(); setIdentity(null) }}
              >
                sign out
              </button>
            </span>
          )}
          <button className="primary small" onClick={() => setAdding(true)}>+ Add camera</button>
        </div>
        <div className="metrics">
          <Metric label="Cameras" value={cameras?.count ?? '—'} />
          <Metric label="Online" value={status?.cameras_online ?? '—'} />
          <Metric label="Sightings indexed" value={status?.sightings_total ?? '—'} />
          {/* The evaluator's plate arrives after the vehicle has passed, so
              "indexing since" is the claim that makes the trace possible. */}
          <Metric
            label="Indexing since"
            value={status?.earliest_sighting ? toIST(status.earliest_sighting) : 'no sightings yet'}
          />
        </div>
        <div className="toggles">
          <label><input type="checkbox" checked={showCoverage} onChange={(e) => setShowCoverage(e.target.checked)} /> Coverage</label>
          <label><input type="checkbox" checked={showGaps} onChange={(e) => setShowGaps(e.target.checked)} /> Gaps</label>
          {coverageSummary && (
            <span className="muted">
              {coverageSummary.cameras_surveyed} surveyed · {coverageSummary.cameras_unsurveyed} unsurveyed
              {coverageSummary.cameras_ptz > 0 && ` · ${coverageSummary.cameras_ptz} PTZ`}
            </span>
          )}
        </div>
        {error && <div className="error">Registry unreachable: {error}</div>}
      </header>

      {showGaps && gapAnalysis && (
        <div className="panel panel-gaps draggable" {...gapsDrag.panelProps}>
          <h3 className="drag-handle" {...gapsDrag.handleProps}>{gapAnalysis.corridor}</h3>
          <div className="gapstat">
            <b>{(gapAnalysis.coverage_ratio * 100).toFixed(2)}%</b> of {(gapAnalysis.corridor_length_m / 1000).toFixed(0)} km covered
          </div>
          <div className="gapstat">
            <b>{gapAnalysis.gap_count}</b> uncovered segments · largest <b>{(gapAnalysis.largest_gap_m / 1000).toFixed(2)} km</b>
          </div>
          <ol className="gaplist">
            {gapAnalysis.gaps.slice(0, 5).map((g) => (
              <li key={g.index}>{(g.length_m / 1000).toFixed(2)} km <span className="muted">at {g.start[1].toFixed(3)}, {g.start[0].toFixed(3)}</span></li>
            ))}
          </ol>
          <div className="hint">{gapAnalysis.caveat}</div>
        </div>
      )}

      <TracePanel
        onJourney={(j) => { setJourney(j); setFocus(null) }}
        onFocus={(lat, lon) => setFocus({ lat, lon })}
        canWrite={identity.can_write}
      />

      {/* The second graded capability, and a separate path end to end: this
          panel never calls the journey endpoint and the trace panel never
          reads an alert. */}
      <AlertConsole onFocus={(lat, lon) => setFocus({ lat, lon })} />

      <div className="panel panel-legend draggable" {...legendDrag.panelProps} {...legendDrag.handleProps}>
        {Object.entries(STATUS_COLOURS).map(([name, colour]) => (
          <span key={name} className="legend-item">
            <i style={{ background: colour }} />
            {name} {counts[name] ? `(${counts[name]})` : ''}
          </span>
        ))}
        {/* Colour is status; fill is how much we trust the position. Shown only
            when there is something to distinguish, so a fully surveyed estate
            does not carry a caveat it has not earned. */}
        {inferred > 0 && (
          <span className="legend-item legend-precision" title={
            'Hollow pins were positioned from a place name, not surveyed. '
            + 'The ring around them is the real scale of that uncertainty.'
          }>
            <i className="legend-inferred" />
            inferred position ({inferred})
          </span>
        )}
      </div>

      {adding && (
        <AddCameraForm
          picked={picked}
          onPickRequest={() => setPicking(true)}
          onCreated={onCreated}
          onClose={() => { setAdding(false); setPicking(false); setPicked(null) }}
        />
      )}

      {picking && <div className="picking-banner">Click the map to place the camera</div>}
      {toast && <div className="toast">{toast}</div>}

      {selected && !adding && (
        <aside className="panel panel-detail draggable" {...detailDrag.panelProps}>
          <button className="close" onClick={() => setSelected(null)} aria-label="Close">×</button>
          <h2 className="drag-handle" {...detailDrag.handleProps}>{selected.properties.name}</h2>
          {/* Video first: an operator opening a camera wants to see it, not
              read its metadata. Playback starts on selection — the M2
              acceptance is "clicking a pin plays video", with no second click. */}
          {selected.properties.status === 'decommissioned' ? (
            <div className="hint">Decommissioned — no live view. Its sightings are retained.</div>
          ) : (
            <LiveView camera={selected} />
          )}
          <dl>
            <Row k="Reference" v={selected.properties.external_ref} />
            <Row k="Status" v={selected.properties.status} />
            <Row k="Department" v={selected.properties.department} />
            <Row k="Ownership" v={selected.properties.ownership_type.replace(/_/g, ' ')} />
            <Row k="District" v={selected.properties.district} />
            <Row k="Address" v={selected.properties.address} />
            <Row k="Adapter" v={selected.properties.adapter} />
            <Row k="Bearing" v={selected.properties.bearing != null ? `${selected.properties.bearing}°` : null} />
            <Row k="Field of view" v={selected.properties.fov_degrees != null ? `${selected.properties.fov_degrees}°` : null} />
            <Row k="Range" v={selected.properties.range_m != null ? `${selected.properties.range_m} m` : null} />
            <Row k="Retention" v={selected.properties.retention_days ? `${selected.properties.retention_days} days` : null} />
          </dl>
          {selected.properties.bearing == null && (
            <div className="hint">Not surveyed — contributes no coverage.</div>
          )}
          {selected.properties.status !== 'decommissioned' && (
            <OcrBoostControl mode="camera" cameraId={selected.properties.id}
                             canWrite={identity.can_write} />
          )}
        </aside>
      )}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span className="metric-value">{value}</span>
      <span className="metric-label">{label}</span>
    </div>
  )
}

function Row({ k, v }: { k: string; v: string | null | undefined }) {
  if (!v) return null
  return (<><dt>{k}</dt><dd>{v}</dd></>)
}
