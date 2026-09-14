/**
 * Make a floating panel draggable by its own body, and remember where it was put.
 *
 * The console is a map with panels floating over it, and several are pinned to
 * the same corner — the camera detail panel and the add-camera form both sit at
 * `top: 12px; right: 12px`, so opening one on top of the other hid it
 * completely. Rather than invent a layout that guesses which panel an operator
 * cares about, let them move it: an operator watching a junction and reading a
 * camera's details wants both, arranged their way.
 *
 * Design notes, in case this looks over-built for a drag:
 *
 * - **Pointer events, not mouse events.** One code path covers mouse, trackpad,
 *   touch and pen, and `setPointerCapture` means a fast drag that leaves the
 *   panel does not drop it — which is exactly what happens when someone flings
 *   a panel to the far side of a large map.
 * - **Position is stored per panel, in localStorage.** A layout that resets on
 *   every reload is a layout nobody bothers to arrange. It is per browser and
 *   never leaves the machine.
 * - **Positions are clamped back into view on load.** A panel dragged to the
 *   edge of a wide monitor and reopened on a laptop would otherwise be parked
 *   off-screen with no way to get it back.
 * - **Only the title bar drags.** The first version made the whole panel a drag
 *   surface and it was much worse to use: clicking just beside an input moved
 *   the panel instead of focusing the field, and any stray drag on the
 *   background shifted a panel somebody was reading. A title bar is the
 *   convention because it is the one region with nothing else to do.
 * - **Dragging never starts on a control**, even in the title bar — the
 *   collapse and close buttons live there.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface Offset { x: number; y: number }

const STORAGE_PREFIX = 'panel-offset:'

/** Controls keep their own behaviour; dragging must not steal their events. */
const INTERACTIVE = 'button, a, input, select, textarea, label, summary, [role="button"]'

function load(key: string): Offset {
  try {
    const raw = window.localStorage.getItem(STORAGE_PREFIX + key)
    if (!raw) return { x: 0, y: 0 }
    const parsed = JSON.parse(raw)
    if (typeof parsed?.x === 'number' && typeof parsed?.y === 'number') return parsed
  } catch {
    /* private windows, cleared site data, or a browser blocking storage */
  }
  return { x: 0, y: 0 }
}

function save(key: string, offset: Offset): void {
  try {
    window.localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(offset))
  } catch {
    /* not worth failing a drag over */
  }
}

/**
 * @param key  stable per panel — this is the localStorage key
 * @returns props to spread onto the panel element, plus a reset
 */
export function useDraggable<T extends HTMLElement = HTMLDivElement>(key: string) {
  // Generic in the element type: the panels are a <form>, a <section> and an
  // <aside> as well as plain divs, and a ref is not covariant — one typed to
  // HTMLElement will not attach to a <form>.
  const ref = useRef<T | null>(null)
  const [offset, setOffset] = useState<Offset>(() => load(key))
  const start = useRef<{ px: number; py: number; ox: number; oy: number } | null>(null)
  const [dragging, setDragging] = useState(false)

  // Pull a panel back if it is parked outside the window — after a resize, or
  // after being arranged on a bigger screen than the one now in use.
  useEffect(() => {
    const clamp = () => {
      const el = ref.current
      if (!el) return
      setOffset((current) => {
        const box = el.getBoundingClientRect()
        // Where the panel would sit with no offset at all.
        const baseLeft = box.left - current.x
        const baseTop = box.top - current.y
        const margin = 24 // always leave this much of the panel reachable
        const minX = -baseLeft + margin - box.width
        const maxX = window.innerWidth - baseLeft - margin
        const minY = -baseTop + margin - box.height
        const maxY = window.innerHeight - baseTop - margin
        const x = Math.min(Math.max(current.x, minX), maxX)
        const y = Math.min(Math.max(current.y, minY), maxY)
        return x === current.x && y === current.y ? current : { x, y }
      })
    }
    clamp()
    window.addEventListener('resize', clamp)
    return () => window.removeEventListener('resize', clamp)
  }, [])

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLElement>) => {
    const target = event.target as HTMLElement
    if (target.closest(INTERACTIVE)) return
    // Let a scrollable region scroll rather than dragging the panel out from
    // under the pointer.
    if (target.closest('[data-no-drag]')) return
    if (event.button !== 0 && event.pointerType === 'mouse') return

    start.current = { px: event.clientX, py: event.clientY, ox: offset.x, oy: offset.y }
    setDragging(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }, [offset])

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLElement>) => {
    const from = start.current
    if (!from) return
    event.preventDefault()
    setOffset({ x: from.ox + (event.clientX - from.px), y: from.oy + (event.clientY - from.py) })
  }, [])

  const end = useCallback(() => {
    if (!start.current) return
    start.current = null
    setDragging(false)
    setOffset((current) => { save(key, current); return current })
  }, [key])

  const reset = useCallback(() => {
    setOffset({ x: 0, y: 0 })
    save(key, { x: 0, y: 0 })
  }, [key])

  return {
    /** Spread on the panel itself: position, and nothing that captures input. */
    panelProps: {
      ref,
      style: {
        transform: offset.x || offset.y ? `translate(${offset.x}px, ${offset.y}px)` : undefined,
        // A panel being moved comes to the front and stays there.
        zIndex: dragging ? 40 : undefined,
        userSelect: dragging ? ('none' as const) : undefined,
      },
    },
    /** Spread on the title bar: this, and only this, is the drag handle. */
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: end,
      onPointerCancel: end,
      // Double-click the title bar to send the panel home.
      onDoubleClick: (event: React.MouseEvent<HTMLElement>) => {
        if ((event.target as HTMLElement).closest(INTERACTIVE)) return
        reset()
      },
      style: {
        cursor: dragging ? 'grabbing' : 'grab',
        touchAction: 'none' as const,
      },
      title: 'Drag to move · double-click to reset',
    },
    moved: offset.x !== 0 || offset.y !== 0,
    reset,
  }
}
