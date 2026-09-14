import { useState } from 'react'
import { useDraggable } from './useDraggable'
import { createCamera } from './api'
import type { NewCamera } from './api'

interface Props {
  /** Position picked by clicking the map, if any. */
  picked: { lat: number; lon: number } | null
  onPickRequest: () => void
  onCreated: (id: string, name: string) => void
  onClose: () => void
}

const ADAPTERS = ['rtsp', 'http', 'hls', 'file', 'onvif', 'vendor_sdk', 'vms_api']
const DEPARTMENTS = ['Police', 'Municipal Corporation', 'GSRTC', 'Panchayat', 'Health']

/**
 * Camera onboarding.
 *
 * One form, no wizard, no steps — onboarding has to be demonstrable in about
 * thirty seconds. Only five fields are required, and position is filled by
 * clicking the map rather than by typing coordinates, which is the difference
 * between a 30-second demo and a 2-minute one.
 *
 * Survey geometry (bearing, field of view, range) is deliberately optional. Real
 * cameras arrive without it, and a registry that refuses them until someone has
 * been out with a compass is a registry nobody uses.
 */
export function AddCameraForm({ picked, onPickRequest, onCreated, onClose }: Props) {
  const drag = useDraggable<HTMLFormElement>('add-camera')
  const [form, setForm] = useState<Partial<NewCamera>>({
    adapter: 'rtsp',
    kind: 'unknown',
    department: 'Police',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const lat = picked?.lat ?? form.lat
  const lon = picked?.lon ?? form.lon
  const ready = !!form.name && !!form.stream_ref && lat != null && lon != null

  const set = (k: keyof NewCamera) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const v = e.target.value
    setForm((f) => ({ ...f, [k]: v === '' ? null : v }))
  }

  const num = (v: unknown) => (v == null || v === '' ? null : Number(v))

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!ready || busy) return
    setBusy(true)
    setError(null)
    try {
      const created = await createCamera({
        name: form.name!,
        adapter: form.adapter || 'rtsp',
        stream_ref: form.stream_ref!,
        lat: lat!,
        lon: lon!,
        department: form.department || null,
        district: form.district || null,
        kind: form.kind || 'unknown',
        bearing: num(form.bearing),
        fov_degrees: num(form.fov_degrees),
        range_m: num(form.range_m),
      })
      onCreated(created.id, created.name)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="panel panel-add draggable" onSubmit={submit} {...drag.panelProps}>
      <button type="button" className="close" onClick={onClose} aria-label="Close">×</button>
      <h2 className="drag-handle" {...drag.handleProps}>Onboard a camera</h2>

      <label>
        Name
        <input autoFocus value={form.name ?? ''} onChange={set('name')} placeholder="Chimanbhai Bridge PTZ" />
      </label>

      <div className="row">
        <label>
          Adapter
          <select value={form.adapter} onChange={set('adapter')}>
            {ADAPTERS.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <label>
          Type
          <select value={form.kind} onChange={set('kind')}>
            <option value="unknown">unknown</option>
            <option value="fixed">fixed</option>
            <option value="ptz">PTZ</option>
          </select>
        </label>
      </div>

      <label>
        Stream URL
        <input value={form.stream_ref ?? ''} onChange={set('stream_ref')} placeholder="rtsp://10.0.0.5:554/stream1" />
        <small>Credentials go in the vault, never in this URL.</small>
      </label>

      <label>
        Position
        <div className="picker">
          <input readOnly value={lat != null && lon != null ? `${lat.toFixed(5)}, ${lon.toFixed(5)}` : ''} placeholder="click the map" />
          <button type="button" onClick={onPickRequest}>Pick on map</button>
        </div>
      </label>

      <div className="row">
        <label>
          Department
          <select value={form.department ?? ''} onChange={set('department')}>
            {DEPARTMENTS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <label>
          District
          <input value={form.district ?? ''} onChange={set('district')} placeholder="Ahmedabad" />
        </label>
      </div>

      <details>
        <summary>Survey geometry (optional)</summary>
        <div className="row">
          <label>Bearing°<input type="number" min={0} max={359} value={form.bearing ?? ''} onChange={set('bearing')} /></label>
          <label>FOV°<input type="number" min={1} max={360} value={form.fov_degrees ?? ''} onChange={set('fov_degrees')} /></label>
          <label>Range m<input type="number" min={1} value={form.range_m ?? ''} onChange={set('range_m')} /></label>
        </div>
        <small>Left blank, the camera is registered but contributes no coverage.</small>
      </details>

      {error && <div className="error">{error}</div>}

      <button type="submit" className="primary" disabled={!ready || busy}>
        {busy ? 'Adding…' : 'Add camera'}
      </button>
    </form>
  )
}
