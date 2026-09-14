import type { StyleSpecification } from 'maplibre-gl'

/**
 * Base map style.
 *
 * Raster OSM tiles rather than a vector style: no API key, no account, and one
 * less thing to fail in front of evaluators. If tiles do not load the camera
 * layer still renders over the background colour, so the demo degrades to
 * "points without a basemap" rather than a blank screen.
 */
export const baseStyle: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '© OpenStreetMap contributors',
      maxzoom: 19,
    },
  },
  layers: [
    { id: 'background', type: 'background', paint: { 'background-color': '#0f1115' } },
    { id: 'osm', type: 'raster', source: 'osm', paint: { 'raster-opacity': 0.85 } },
  ],
}

/** Status drives pin colour; the operator reads the map before the table. */
export const STATUS_COLOURS: Record<string, string> = {
  online: '#31c48d',
  degraded: '#f0a63a',
  offline: '#e5484d',
  unknown: '#7c8497',
}

/**
 * How far a pin might really be from where it is drawn, in metres.
 *
 * The registry knows the difference between a position an operator surveyed and
 * one this platform inferred from a place name, and the map has to show it.
 * A district-centroid pin rendered identically to a surveyed one claims a
 * precision the registry does not have — and coverage polygons, gap analysis
 * and route plausibility are all computed from these positions, so overstating
 * them overstates every conclusion downstream.
 *
 * The radii are the honest scale of each match, not decoration: a district
 * centroid really can be fifteen kilometres from the camera.
 */
export const PRECISION_RADIUS_M: Record<string, number> = {
  survey: 0,        // an operator placed it; draw no uncertainty
  landmark: 250,
  city: 3000,
  district: 15000,
  unplaced: 0,      // nothing to draw a radius around — flagged instead
}

export const PRECISION_LABELS: Record<string, string> = {
  survey: 'surveyed position',
  landmark: 'matched a landmark (±250 m)',
  city: 'matched a city (±3 km)',
  district: 'district centroid only (±15 km)',
  unplaced: 'no position — needs a survey',
}
