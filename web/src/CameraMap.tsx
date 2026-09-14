import { useEffect, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import type { ExpressionSpecification } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { CameraCollection, CameraFeature } from './api'
import { baseStyle, PRECISION_RADIUS_M, STATUS_COLOURS } from './mapStyle'

interface Props {
  cameras: CameraCollection | null
  coverage: GeoJSON.FeatureCollection | null
  gaps: GeoJSON.FeatureCollection | null
  showCoverage: boolean
  showGaps: boolean
  /** When true, the next map click reports a position instead of selecting. */
  picking: boolean
  /** The traced vehicle's route, or null when nothing is being traced. */
  journey: GeoJSON.FeatureCollection | null
  /** Set to fly the map to a position, e.g. a clicked history row. */
  focus: { lat: number; lon: number } | null
  onPick: (lat: number, lon: number) => void
  onSelect: (camera: CameraFeature | null) => void
}

/**
 * Camera layer.
 *
 * Rendered as MapLibre GL circle layers rather than DOM markers: at the camera
 * densities this platform claims to support (~80,000 statewide) one DOM node
 * per camera stops being viable, which is the reason for MapLibre over Leaflet.
 * Everything below stays inside the GL layer for the same reason.
 */
export function CameraMap({
  cameras, coverage, gaps, showCoverage, showGaps, picking, journey, focus,
  onPick, onSelect,
}: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<maplibregl.Map | null>(null)
  // Handlers are registered once on the GL layer, so they are read through refs
  // rather than re-bound on every render.
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect
  const onPickRef = useRef(onPick)
  onPickRef.current = onPick
  const [mapError, setMapError] = useState<string | null>(null)
  const pickingRef = useRef(picking)
  pickingRef.current = picking

  useEffect(() => {
    if (!container.current || map.current) return

    // MapLibre throws in its constructor when the browser has no WebGL (GPU
    // disabled, remote desktop, some VMs). Uncaught, that unmounted the whole
    // console to a black page — found 14 Sep 2026 in a Chrome running with
    // --use-gl=disabled. The map is one panel; trace, alerts and export still
    // work without it, so it says so and steps aside.
    let m: maplibregl.Map
    try {
      m = new maplibregl.Map({
        container: container.current,
        style: baseStyle,
        center: [72.9, 22.1], // NH-48 corridor, Ahmedabad to Surat
        zoom: 7.2,
        attributionControl: { compact: true },
      })
    } catch (e) {
      setMapError(e instanceof Error ? e.message : String(e))
      return
    }
    m.addControl(new maplibregl.NavigationControl({}), 'top-right')
    m.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }))
    map.current = m
    // Dev-only handle, so the live layer state can be inspected from the
    // console instead of guessed at from screenshots.
    if (import.meta.env.DEV) (window as unknown as { __map: maplibregl.Map }).__map = m

    return () => {
      m.remove()
      map.current = null
    }
  }, [])

  useEffect(() => {
    const m = map.current
    if (!m || !cameras) return

    const apply = () => {
      const existing = m.getSource('cameras') as maplibregl.GeoJSONSource | undefined
      if (existing) {
        existing.setData(cameras as GeoJSON.FeatureCollection)
        return
      }

      m.addSource('cameras', { type: 'geojson', data: cameras as GeoJSON.FeatureCollection })

      // Coverage wedge stand-in. bearing/fov/range are not decoration — they
      // drive coverage and route plausibility — so the map shows range even
      // before M1 draws true wedges.
      m.addLayer({
        id: 'camera-range',
        type: 'circle',
        source: 'cameras',
        paint: {
          'circle-radius': [
            'interpolate', ['linear'], ['zoom'],
            8, 3,
            14, ['/', ['coalesce', ['get', 'range_m'], 50], 4],
          ],
          'circle-color': ['coalesce',
            ['get', ['get', 'status'], ['literal', STATUS_COLOURS]],
            STATUS_COLOURS.unknown,
          ],
          'circle-opacity': 0.12,
        },
      })

      // Positional uncertainty, drawn to scale.
      //
      // A pin the platform inferred from a place name is not the same claim as
      // one an operator surveyed, and rendering them identically overstates
      // what the registry knows — which matters, because coverage polygons and
      // route plausibility are computed from these positions. The radius is in
      // real metres, so a district centroid visibly covers a district.
      //
      // Drawn beneath the pins, and only where there is uncertainty to draw:
      // a surveyed camera gets a radius of zero and disappears from this layer
      // on its own, with no filter to keep in sync.
      // 156543 m/px at zoom 0 on the equator; Gujarat sits near 22.5°N, hence
      // the cos() term. Close enough for an honesty cue.
      const K = 156543 * Math.cos((22.5 * Math.PI) / 180)
      const METRES: ExpressionSpecification = [
        'coalesce',
        ['get',
          ['coalesce', ['get', 'geo_precision'], 'unplaced'],
          ['literal', PRECISION_RADIUS_M],
        ],
        0,
      ]

      m.addLayer({
        // The camera's positional uncertainty in metres, from its precision
        // tag. The *key* is coalesced too: `geo_precision` is null on rows that
        // predate the column, and `['get', null, …]` is not a lookup MapLibre
        // will evaluate.
        id: 'camera-uncertainty',
        type: 'circle',
        source: 'cameras',
        paint: {
          // metres → pixels, as an `interpolate` on zoom.
          //
          // The obvious form — dividing by `['^', 2, ['zoom']]` inline — is
          // rejected: MapLibre allows `zoom` only at the top level of a `step`
          // or `interpolate`. It does not merely ignore the layer either; the
          // throw aborts the rest of the `load` handler, so every layer added
          // after this one was silently missing too. Caught by opening the page,
          // not by the typechecker.
          //
          // Metres per pixel at zoom z is 156543·cos(lat)/2^z, so the radius in
          // pixels is metres·2^z/K. An exponential-base-2 interpolation between
          // z0 and z22 reproduces that curve exactly, which is what the idiom
          // is for.
          'circle-radius': [
            'interpolate',
            ['exponential', 2],
            ['zoom'],
            0, ['/', METRES, K],
            22, ['/', ['*', METRES, 2 ** 22], K],
          ],
          'circle-color': '#7c8497',
          'circle-opacity': 0.07,
          'circle-stroke-width': 1,
          'circle-stroke-color': '#7c8497',
          'circle-stroke-opacity': 0.35,
        },
      })

      m.addLayer({
        id: 'camera-points',
        type: 'circle',
        source: 'cameras',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 3.5, 12, 7],
          'circle-color': ['coalesce',
            ['get', ['get', 'status'], ['literal', STATUS_COLOURS]],
            STATUS_COLOURS.unknown,
          ],
          // A surveyed pin is drawn solid; an inferred one is hollowed out, so
          // the distinction survives at statewide zoom where the uncertainty
          // ring is a sub-pixel smudge.
          'circle-opacity': [
            'case', ['==', ['get', 'geo_precision'], 'survey'], 1, 0.45,
          ],
          'circle-stroke-width': 1.5,
          'circle-stroke-color': [
            'case', ['==', ['get', 'geo_precision'], 'survey'], '#0f1115', '#e8ecf4',
          ],
        },
      })

      m.on('click', 'camera-points', (e) => {
        if (pickingRef.current) return
        const f = e.features?.[0]
        if (f) onSelectRef.current(f as unknown as CameraFeature)
      })
      m.on('mouseenter', 'camera-points', () => {
        m.getCanvas().style.cursor = 'pointer'
      })
      m.on('mouseleave', 'camera-points', () => {
        m.getCanvas().style.cursor = ''
      })
      m.on('click', (e) => {
        // Picking a position for onboarding takes priority over selection:
        // clicking the map to place a camera is what keeps onboarding inside
        // its 30-second budget.
        if (pickingRef.current) {
          onPickRef.current(e.lngLat.lat, e.lngLat.lng)
          return
        }
        const hits = m.queryRenderedFeatures(e.point, { layers: ['camera-points'] })
        if (hits.length === 0) onSelectRef.current(null)
      })
    }

    // `style.load`, not `load`: `load` waits for the first complete render,
    // which includes basemap tiles. When the tile host is unreachable — a
    // firewalled control room, or a venue with no outbound internet — those
    // requests hang and `load` never fires, leaving a blank screen with 82
    // cameras behind it. Style parse is all the camera layers actually need,
    // so the map degrades to "pins without a basemap" as intended.
    if (m.isStyleLoaded()) apply()
    else m.once('style.load', apply)
  }, [cameras])

  // Coverage and gap overlays, kept in their own effect so toggling them does
  // not re-create the camera layers.
  //
  // This effect creates its own sources and layers rather than relying on the
  // camera effect having run first. React does not guarantee that ordering, and
  // when the overlay effect won the race the sources did not exist yet, setData
  // was silently skipped, and the gap lines never appeared with no error.
  useEffect(() => {
    const m = map.current
    if (!m) return

    const apply = () => {
      const empty: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

      if (!m.getSource('coverage')) {
        m.addSource('coverage', { type: 'geojson', data: empty })
        // Insert beneath the camera pins so a wedge never hides its own camera.
        const below = m.getLayer('camera-range') ? 'camera-range' : undefined
        m.addLayer({
          id: 'coverage-fill',
          type: 'fill',
          source: 'coverage',
          layout: { visibility: 'none' },
          paint: {
            // PTZ coverage is drawn differently on purpose: its wedge is where
            // the camera happens to be aimed, not what it permanently covers.
            'fill-color': ['case', ['get', 'variable'], '#c084fc', '#4f9cf9'],
            'fill-opacity': 0.25,
          },
        }, below)
        m.addLayer({
          id: 'coverage-line',
          type: 'line',
          source: 'coverage',
          layout: { visibility: 'none' },
          paint: {
            'line-color': ['case', ['get', 'variable'], '#c084fc', '#4f9cf9'],
            'line-width': 0.8,
            'line-opacity': 0.6,
          },
        }, below)
      }

      if (!m.getSource('gaps')) {
        m.addSource('gaps', { type: 'geojson', data: empty })
        const below = m.getLayer('camera-range') ? 'camera-range' : undefined
        m.addLayer({
          id: 'gaps-line',
          type: 'line',
          source: 'gaps',
          layout: { visibility: 'none', 'line-cap': 'round' },
          paint: {
            'line-color': '#e5484d',
            'line-width': ['interpolate', ['linear'], ['zoom'], 6, 2.5, 12, 6],
            'line-opacity': 0.8,
          },
        }, below)
      }

      const cov = m.getSource('coverage') as maplibregl.GeoJSONSource | undefined
      if (cov && coverage) cov.setData(coverage)
      const gap = m.getSource('gaps') as maplibregl.GeoJSONSource | undefined
      if (gap && gaps) gap.setData(gaps)
      for (const [id, on] of [
        ['coverage-fill', showCoverage],
        ['coverage-line', showCoverage],
        ['gaps-line', showGaps],
      ] as const) {
        if (m.getLayer(id)) m.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none')
      }
    }
    // Style parse, not full render — see the camera effect above.
    if (m.isStyleLoaded()) apply()
    else m.once('style.load', apply)
  }, [coverage, gaps, showCoverage, showGaps])

  // The traced route. Its own sources for the same reason as the overlays: React
  // does not order effects, and a setData against a source that does not exist
  // yet is silently ignored rather than raising.
  useEffect(() => {
    const m = map.current
    if (!m) return

    const apply = () => {
      const empty: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

      if (!m.getSource('journey')) {
        m.addSource('journey', { type: 'geojson', data: empty })
        // A casing line under a brighter core, so the route stays legible over
        // both the basemap and the coverage wedges.
        m.addLayer({
          id: 'journey-casing',
          type: 'line',
          source: 'journey',
          filter: ['==', ['geometry-type'], 'LineString'],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#0b0d10', 'line-width': 7, 'line-opacity': 0.6 },
        })
        // Two line layers rather than one with a conditional dash, because
        // `line-dasharray` is not a data-driven property in MapLibre: passing an
        // expression fails validation with "data expressions not supported", the
        // layer is never added, and the route silently does not draw. Filtering
        // by the same property across two layers gets the same result honestly.
        m.addLayer({
          id: 'journey-line',
          type: 'line',
          source: 'journey',
          filter: ['all', ['==', ['geometry-type'], 'LineString'],
                   ['==', ['get', 'road_snapped'], true]],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#f5a524', 'line-width': 3.5 },
        })
        m.addLayer({
          id: 'journey-line-fallback',
          type: 'line',
          source: 'journey',
          filter: ['all', ['==', ['geometry-type'], 'LineString'],
                   ['!=', ['get', 'road_snapped'], true]],
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          // Dashed, so an operator can see at a glance that this segment is a
          // straight-line guess rather than a road.
          paint: { 'line-color': '#f5a524', 'line-width': 3.5, 'line-dasharray': [2, 1.5] },
        })
        // Direction is carried by colour, not by numbered labels: a `symbol`
        // layer needs a `glyphs` URL in the style, and this style deliberately
        // has none — a font server is one more thing to be unreachable from a
        // firewalled control room. First visit green, last red, the rest amber;
        // the movement-history table carries the sequence numbers.
        m.addLayer({
          id: 'journey-visits',
          type: 'circle',
          source: 'journey',
          filter: ['==', ['get', 'kind'], 'sighting'],
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 6, 12, 11],
            'circle-color': [
              'case',
              ['==', ['get', 'sequence'], 1], '#30a46c',
              ['==', ['get', 'is_last'], true], '#e5484d',
              '#f5a524',
            ],
            'circle-stroke-color': '#0b0d10',
            'circle-stroke-width': 2,
          },
        })
      }

      const src = m.getSource('journey') as maplibregl.GeoJSONSource | undefined
      if (src) src.setData(journey ?? empty)
    }
    if (m.isStyleLoaded()) apply()
    else m.once('style.load', apply)
  }, [journey])

  // Focus a specific position — a clicked row in the movement history.
  useEffect(() => {
    const m = map.current
    if (m && focus) m.flyTo({ center: [focus.lon, focus.lat], zoom: 13, duration: 800 })
  }, [focus])

  // A crosshair is the only affordance telling the operator the map is waiting
  // for a click rather than ready to select.
  useEffect(() => {
    const m = map.current
    if (m) m.getCanvas().style.cursor = picking ? 'crosshair' : ''
  }, [picking])

  return (
    <>
      <div ref={container} style={{ position: 'absolute', inset: 0 }} />
      {mapError && (
        <div className="map-error">
          <strong>The map needs WebGL, which this browser has turned off.</strong>
          <p>Enable graphics acceleration in the browser settings, or open this page in
            another browser. Trace, alerts and reports below still work.</p>
        </div>
      )}
    </>
  )
}
