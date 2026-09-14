/**
 * API client. The base URL is injected at build time so the same image can be
 * pointed at a hosted deployment without a rebuild of the source.
 */

const BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

/** GeoJSON Feature for one camera, as the registry returns it. */
export interface CameraFeature {
  type: 'Feature'
  geometry: { type: 'Point'; coordinates: [number, number] }
  properties: {
    id: string
    external_ref: string | null
    name: string
    department: string | null
    ownership_type: string
    adapter: string
    stream_ref: string
    address: string | null
    district: string | null
    bearing: number | null
    fov_degrees: number | null
    range_m: number | null
    status: string
    /** How the position was arrived at: survey | landmark | city | district | unplaced. */
    geo_precision: string | null
    last_seen: string | null
    retention_days: number | null
  }
}

export interface CameraCollection {
  type: 'FeatureCollection'
  features: CameraFeature[]
  count: number
}

export interface PlatformStatus {
  started_at: number
  uptime_s: number
  cameras_total: number
  cameras_online: number
  sightings_total: number
  earliest_sighting: string | null
  latest_sighting: string | null
  /** The database's clock. Client clocks are not the reference for anything here. */
  now: string
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`${path} returned ${res.status}`)
  return res.json() as Promise<T>
}

export const fetchCameras = () => get<CameraCollection>('/api/cameras/geojson')
export const fetchStatus = () => get<PlatformStatus>('/api/status')

/** Timestamps are stored UTC and rendered IST — a project-wide convention. */
export function toIST(iso: string): string {
  return new Date(iso).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    dateStyle: 'medium',
    timeStyle: 'medium',
  })
}


// --- coverage ----------------------------------------------------------

export interface CoverageSummary {
  cameras_total: number
  cameras_surveyed: number
  cameras_unsurveyed: number
  cameras_ptz: number
  covered_area_km2: number
}

export interface GapSegment {
  index: number
  length_m: number
  start: [number, number]
  end: [number, number]
}

export interface GapAnalysis {
  corridor: string
  corridor_length_m: number
  covered_length_m: number
  uncovered_length_m: number
  coverage_ratio: number
  gap_count: number
  largest_gap_m: number
  gaps: GapSegment[]
  geojson: GeoJSON.FeatureCollection
  caveat: string
}

export interface NewCamera {
  name: string
  adapter: string
  stream_ref: string
  lat: number
  lon: number
  department?: string | null
  district?: string | null
  kind?: string
  bearing?: number | null
  fov_degrees?: number | null
  range_m?: number | null
  external_ref?: string | null
}

export const fetchCoverage = () => get<GeoJSON.FeatureCollection>('/api/coverage/geojson')
export const fetchCoverageSummary = () => get<CoverageSummary>('/api/coverage/summary')
export const fetchGaps = () => get<GapAnalysis>('/api/coverage/gaps')

/** Onboard a camera. The registry is the only way one enters the platform. */
export async function createCamera(body: NewCamera, actor = 'operator'): Promise<CameraFeature['properties']> {
  const res = await fetch(`${BASE}/api/cameras`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json', 'X-Actor': actor }),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    // Surface the API's own message: it explains *why* (unknown department,
    // credentials embedded in the URL), which a generic error would discard.
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      if (typeof err.detail === 'string') detail = err.detail
      else if (Array.isArray(err.detail)) detail = err.detail.map((d: {msg: string}) => d.msg).join('; ')
    } catch { /* keep the status-code fallback */ }
    throw new Error(detail)
  }
  return res.json()
}


// --- live view ---------------------------------------------------------

export interface StreamTarget {
  camera_id: string
  camera_name: string
  adapter: string
  /** `webrtc`, `hls` or `file`. The player branches on this, nothing else. */
  protocol: string
  url: string
  /** False means the relay is still starting — retry rather than fail. */
  ready: boolean
  /** The relay keeps dying and restarting: the stream may connect and then
   *  deliver nothing. `ready` cannot express that, so it is reported apart. */
  unstable: boolean
  relayed: boolean
  /** True when the platform re-encoded this camera so a browser could play it. */
  transcoded: boolean
  detail: string | null
}

/**
 * Resolve a camera to something playable.
 *
 * `caseRef` is optional but worth sending: it binds this access to an
 * investigation in the audit trail, which is what makes viewing a public-space
 * camera defensible rather than merely logged.
 */
export async function fetchStream(
  id: string, actor = 'operator', caseRef?: string,
): Promise<StreamTarget> {
  const headers: Record<string, string> = authHeaders({ 'X-Actor': actor })
  if (caseRef) headers['X-Case-Ref'] = caseRef
  const res = await fetch(`${BASE}/api/cameras/${id}/stream`, { headers })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      if (typeof err.detail === 'string') detail = err.detail
    } catch { /* keep the status-code fallback */ }
    throw new Error(detail)
  }
  return res.json()
}


// --- M4: trace and journey ------------------------------------------------

/** One camera's observation of the traced vehicle, over an interval. */
export interface JourneyVisit {
  kind: 'sighting'
  sequence: number
  camera_id: string
  camera_name: string | null
  district: string | null
  ts: string
  last_seen: string
  dwell_s: number
  sightings: number
  sighting_ids: number[]
  confidence: number
  thumbnail_url: string | null
  /** Vehicle ids and how they were linked: plate, appearance (a lead) or new. */
  vehicle_uids: number[]
  uid_via: string[]
  uid_distance: number | null
}

/** One hop between consecutive visits. `plausible` is the clone signal. */
export interface JourneySegment {
  from_camera: string
  to_camera: string
  /** Best available: the road distance where OSRM could route the pair. */
  distance_m: number
  /** Great-circle. What plausibility is judged on — see `plausibility_basis`. */
  direct_distance_m: number
  road_distance_m: number | null
  elapsed_s: number
  /** From the direct distance, so it is a lower bound on the true speed. */
  implied_speed_kmh: number | null
  road_speed_kmh: number | null
  plausible: boolean
  plausibility_basis: string
  road_snapped: boolean
  note: string | null
}

export interface JourneyProperties {
  plate: string
  confidence: number
  summary: string
  sightings: number
  visits: number
  cameras: number
  distance_m: number
  elapsed_s: number
  first_seen: string | null
  last_seen: string | null
  road_snapped: boolean
  legs: { sequence: number; cameras: string[]; started: string; ended: string }[]
  implausible_transitions: number
  segments: JourneySegment[]
  query_ms: number
}

export interface Journey extends GeoJSON.FeatureCollection {
  properties: JourneyProperties
}

export interface SightingHit {
  id: number
  ts: string
  camera_id: string
  camera_name: string | null
  plate_raw: string
  plate_normalised: string
  confidence: number
  format_valid: boolean
}

/**
 * Exact-then-fuzzy plate lookup. The normalisation that ran when the sighting
 * was written runs again here, so `GJ 01 AB 1234` finds `GJO1AB1234`.
 */
export async function searchPlates(plate: string, actor = 'operator'): Promise<SightingHit[]> {
  const res = await fetch(`${BASE}/api/sightings/search?plate=${encodeURIComponent(plate)}`, {
    headers: authHeaders({ 'X-Actor': actor }),
  })
  if (!res.ok) throw new Error(`plate search failed: ${res.status}`)
  return res.json()
}

/**
 * The retrospective trace. One request returns the route, the visits and the
 * per-hop plausibility — the map, the timeline and the export all read from it.
 * `caseRef` is bound into the audit trail, which is what makes a trace evidence
 * rather than a query.
 */
export async function fetchJourney(
  plate: string,
  opts: { from?: string; to?: string; caseRef?: string; actor?: string } = {},
): Promise<Journey> {
  const params = new URLSearchParams()
  if (opts.from) params.set('from', opts.from)
  if (opts.to) params.set('to', opts.to)
  const query = params.toString()
  const headers: Record<string, string> = authHeaders({ 'X-Actor': opts.actor ?? 'operator' })
  if (opts.caseRef) headers['X-Case-Ref'] = opts.caseRef

  const res = await fetch(
    `${BASE}/api/vehicles/${encodeURIComponent(plate)}/journey${query ? `?${query}` : ''}`,
    { headers },
  )
  if (!res.ok) throw new Error(`journey failed: ${res.status}`)
  return res.json()
}


// --- OCR boost: PaddleOCR-VL on chosen cameras, for a while ----------------

export interface OcrBoost {
  id: number
  camera_id: string
  camera_ref: string | null
  camera_name: string
  backend: string
  /** pending: no worker has picked it up yet · running · failed (see apply_error)
   *  · expired · cleared */
  status: 'pending' | 'running' | 'failed' | 'expired' | 'cleared'
  requested_by: string
  case_ref: string | null
  reason: string | null
  requested_at: string
  expires_at: string
  applied_at: string | null
  apply_error: string | null
}

export type BoostTarget =
  | { camera_ids: string[] }
  | { near: { lat: number; lon: number; radius_m: number } }

async function errorDetail(res: Response): Promise<string> {
  try {
    const err = await res.json()
    if (typeof err.detail === 'string') return err.detail
    if (Array.isArray(err.detail)) return err.detail.map((d: { msg: string }) => d.msg).join('; ')
  } catch { /* fall through to the status code */ }
  return `HTTP ${res.status}`
}

export const fetchBoosts = () => get<OcrBoost[]>('/api/ocr-boosts')

/** Read the chosen cameras with PaddleOCR-VL until the boost expires. Operators only. */
export async function startBoost(
  target: BoostTarget, opts: { minutes: number; case_ref?: string; reason?: string },
): Promise<OcrBoost[]> {
  const res = await fetch(`${BASE}/api/ocr-boosts`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ ...target, ...opts }),
  })
  if (!res.ok) throw new Error(await errorDetail(res))
  return res.json()
}

export async function clearBoost(cameraId: string): Promise<void> {
  const res = await fetch(`${BASE}/api/ocr-boosts/${cameraId}`, {
    method: 'DELETE', headers: authHeaders(),
  })
  // 404 means it had already ended, which is what the operator wanted.
  if (!res.ok && res.status !== 404) throw new Error(await errorDetail(res))
}


// --- M5: watchlist and live alerting ---------------------------------------

export interface WatchlistEntry {
  id: string
  plate: string
  plate_normalised: string
  category: string
  severity: number
  source: string | null
  case_ref: string | null
  notes: string | null
  active: boolean
  created_at: string
  /** How many times this entry has actually fired — a list nobody has checked
   *  looks identical to a live one without it. */
  alerts: number
}

export interface Alert {
  id: string
  sighting_id: number
  raised_at: string
  sighting_ts: string
  /**
   * confirmed | probable | possible | attribute — see the tier table in the
   * API docs. `attribute` means no plate was read: the vehicle merely matches
   * the description of a wanted one, and only ever after that vehicle's plate
   * was matched minutes earlier.
   */
  tier: string
  priority: number
  status: string
  acknowledged_by: string | null
  acknowledged_at: string | null
  resolution_note: string | null
  watchlist_id: string
  plate: string
  category: string
  severity: number
  case_ref: string | null
  plate_read: string
  confidence: number
  camera_id: string
  camera_name: string | null
  district: string | null
  lat: number | null
  lon: number | null
  thumbnail_url: string | null
  detection_latency_s: number
}

export interface AlertStats {
  window_hours: number
  watchlist_entries: number
  watchlist_active: number
  alerts: number
  unacknowledged: number
  entries_fired: number
  cameras: number
  p50_latency_s: number | null
  p95_latency_s: number | null
  max_latency_s: number | null
  by_tier: { tier: string; alerts: number; mean_priority: number }[]
  by_status: { status: string; alerts: number }[]
  caveat: string
}

export const fetchAlerts = (limit = 50) => get<Alert[]>(`/api/alerts?limit=${limit}`)
export const fetchAlertStats = () => get<AlertStats>('/api/alerts/stats')
export const fetchWatchlist = () => get<WatchlistEntry[]>('/api/watchlist')

/** Absolute URL for an evidence crop; the API returns a relative path. */
export const cropUrl = (path: string) => `${BASE}${path}`

/**
 * Arm the alerting path for a plate. Every ANPR worker picks the entry up
 * within its watchlist refresh (2 s), so the next sighting of the vehicle
 * raises an alert — which is exactly what the M5 acceptance measures.
 */
export async function addToWatchlist(
  body: { plate: string; category?: string; severity?: number; case_ref?: string; notes?: string },
  actor = 'operator',
): Promise<WatchlistEntry> {
  const res = await fetch(`${BASE}/api/watchlist`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json', 'X-Actor': actor }),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      if (typeof err.detail === 'string') detail = err.detail
      else if (Array.isArray(err.detail)) detail = err.detail.map((d: { msg: string }) => d.msg).join('; ')
    } catch { /* keep the status-code fallback */ }
    throw new Error(detail)
  }
  return res.json()
}

/**
 * Acknowledge, action or dismiss. Sent over REST rather than the socket
 * deliberately: it must be audited, and it must survive a dropped connection.
 */
export async function setAlertStatus(
  id: string, status: string, note?: string, actor = 'operator',
): Promise<Alert> {
  const res = await fetch(`${BASE}/api/alerts/${id}/status`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json', 'X-Actor': actor }),
    body: JSON.stringify({ status, note }),
  })
  if (!res.ok) throw new Error(`could not update alert: ${res.status}`)
  return res.json()
}

/**
 * WebSocket URL for the live alert feed, derived from the API base.
 *
 * `BASE` is deliberately the empty string on a hosted instance — that is what
 * makes every `fetch` above same-origin and relative, and it is set that way
 * in `web/Dockerfile.prod`. Every other caller concatenates it and is fine.
 * This one cannot: a WebSocket needs an absolute URL, and `new URL(path, '')`
 * throws `Invalid base URL` rather than resolving against the document.
 *
 * That threw during render and took the **entire app** down to a blank page on
 * the hosted build — found 6 Sep 2026 by opening the public URL in a browser,
 * having not been found by any amount of `curl`, which never runs the
 * JavaScript. Falling back to the document's own origin is the same
 * same-origin intent, expressed in the absolute form this API requires.
 */
export function alertSocketUrl(): string {
  const url = new URL('/api/alerts/live', BASE || window.location.origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}


// --- M6: report export -----------------------------------------------------

/**
 * The detection report (Deliverable 4), as a browser download.
 *
 * Built as a URL rather than fetched and blobbed: the browser's own download
 * handles a multi-megabyte PDF without holding it in memory, shows real
 * progress, and honours the Content-Disposition filename the API already sets.
 * The case reference cannot be sent as a header this way, so it rides as a
 * query parameter the API also accepts — an export is audited either way, and
 * losing the case binding to save a parameter would be the wrong trade.
 */
export function reportUrl(
  format: 'pdf' | 'csv',
  opts: {
    plate?: string; district?: string; since?: string; until?: string;
    limit?: number; case_ref?: string; actor?: string
  } = {},
): string {
  const params = new URLSearchParams({ format, actor: opts.actor ?? 'operator' })
  for (const [key, value] of Object.entries(opts)) {
    if (value !== undefined && value !== '') params.set(key, String(value))
  }
  return `${BASE}/api/reports/detections?${params.toString()}`
}


// --- M7: performance evidence ----------------------------------------------

export interface StageLatency {
  stage: string
  samples: number
  p50_ms: number
  p95_ms: number
  max_ms: number
}

export interface RouteLatency {
  route: string
  requests: number
  errors: number
  samples: number
  p50_ms: number
  p95_ms: number
  max_ms: number
}

export interface Performance {
  window_minutes: number
  uptime_s: number
  cameras_registered: number
  cameras_online: number
  cameras_degraded: number
  cameras_processing: number
  frames_decoded: number
  frames_analysed: number
  analysed_fraction: number | null
  detector_load_avoided: string | null
  vehicles_tracked: number
  plate_reads: number
  sightings_written: number
  mean_decode_fps: number | null
  sightings_indexed: number
  distinct_plates: number
  sightings_per_minute: number
  alerts_raised: number
  alert_p95_latency_s: number | null
  api_latency: RouteLatency[]
  pipeline_counters: Record<string, number>
  write_health: {
    sightings_produced: number
    sightings_write_failed: number
    write_failure_fraction: number
    healthy: boolean
    detail: string
  }
  load_shedding: {
    ocr_shed: number
    ocr_shed_fraction: number
    ocr_skipped_settled: number
    ocr_skipped_fraction: number
    sessions_via_relay: number
    motion_fallback_windows: number
    overlay_suppressed: number
  }
  stages: StageLatency[]
  per_camera: {
    external_ref: string
    name: string
    frames_decoded: number
    frames_analysed: number
    sightings_written: number
    decode_fps: number | null
  }[]
}

export interface CameraCapability {
  camera_id: string
  name: string | null
  external_ref: string | null
  district: string | null
  status: string | null
  grade: string
  reason: string
  counts_toward_accuracy: boolean
  sightings: number
  median_plate_px: number | null
  mean_chars: number
  identifying_fraction: number
  format_valid_fraction: number
  wide_enough_fraction: number
}

export interface CapabilityReport {
  window_days: number
  cameras_assessed: number
  summary: Record<string, number>
  anpr_plate_px_floor: number
  min_samples: number
  accuracy_scope: string
  cameras: CameraCapability[]
}

export const fetchPerformance = (minutes = 15) =>
  get<Performance>(`/api/performance?minutes=${minutes}`)
export const fetchCapability = () =>
  get<CapabilityReport>('/api/cameras/anpr-capability?days=1')


// --- M8: authentication ----------------------------------------------------

export interface Identity {
  username: string
  role: string
  can_write: boolean
  /** False when the API runs in local development mode with auth disabled. */
  auth_required: boolean
}

const TOKEN_KEY = 'cctv.token'

/**
 * The bearer token, kept in localStorage.
 *
 * sessionStorage would be tidier for a shared machine, but a control room
 * leaves this open across shifts and a tab reload is not a logout. The token is
 * short-lived (8 hours) precisely because it is stored where a stored token can
 * be read.
 */
export const getToken = () => localStorage.getItem(TOKEN_KEY)
export const setToken = (token: string | null) =>
  token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY)

export function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken()
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra
}

export const fetchIdentity = () => get<Identity>('/api/auth/me')

export async function login(username: string, password: string): Promise<Identity> {
  const res = await fetch(`${BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    // The API answers one message for both an unknown account and a wrong
    // password, and the UI must not helpfully distinguish them either.
    throw new Error(res.status === 401 ? 'Unknown account or wrong password' : `HTTP ${res.status}`)
  }
  const session = await res.json()
  setToken(session.token)
  return { ...session, can_write: session.role === 'operator', auth_required: true }
}

export function logout(): void {
  // Tokens are signed rather than stored server-side, so discarding it here is
  // the whole of a logout. An endpoint that pretended otherwise would be theatre.
  setToken(null)
}


/**
 * Download a report, carrying the session token.
 *
 * A plain `<a href>` cannot set an `Authorization` header, so under
 * authentication the export links returned 401 — the same defect as the
 * evidence crops, found the same way. Fetching it here and handing the browser
 * a blob keeps the token out of the URL, and it lets the actor and case
 * reference travel as headers again rather than as query parameters.
 *
 * The trade is that the file is held in memory before it is saved. At the
 * export limits offered (500 rows as PDF, 5,000 as CSV) that is a few megabytes.
 */
export async function downloadReport(
  format: 'pdf' | 'csv',
  opts: { plate?: string; district?: string; since?: string; until?: string;
          limit?: number; case_ref?: string; actor?: string } = {},
): Promise<void> {
  const { case_ref, actor, ...filters } = opts
  const headers = authHeaders({ 'X-Actor': actor ?? 'operator' })
  if (case_ref) headers['X-Case-Ref'] = case_ref

  const res = await fetch(reportUrl(format, filters), { headers })
  if (!res.ok) throw new Error(`export failed: ${res.status}`)

  const name = /filename="([^"]+)"/.exec(
    res.headers.get('content-disposition') ?? '',
  )?.[1] ?? `detections.${format}`

  const url = URL.createObjectURL(await res.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  // Revoked on the next frame: revoking immediately can cancel the save in
  // some browsers before it has read the blob.
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
