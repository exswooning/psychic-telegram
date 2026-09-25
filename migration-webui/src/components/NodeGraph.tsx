/**
 * A node-editor canvas: boxes with typed sockets joined by bezier wires, in
 * labelled frames, that you can pan, zoom, and trace through -- the look and
 * feel of Blender's node editor, because "what feeds what" reads faster as
 * wiring than as a list. Pure SVG; a graph library would be a dependency for
 * ~200 lines of geometry.
 *
 * Purely presentational. What the nodes are, and which numbers sit on them,
 * is the caller's business (see pages/Pipeline.tsx) -- nothing here invents a
 * value.
 *
 * Interaction: drag to pan; scroll to pan; ctrl/cmd + scroll (or a trackpad
 * pinch) or the buttons to zoom; click a node to select it; click a frame's
 * title to zoom to it. Small text drops out when zoomed far out, so the whole
 * picture stays legible as a picture.
 */
import React, {
  forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState,
} from 'react'
import { Box, Chip, IconButton, Stack, Tooltip } from '@mui/material'
import { Add as ZoomInIcon, FitScreen as FitIcon, Remove as ZoomOutIcon } from '@mui/icons-material'
import { HEAD, LIVE, NODE_W, ROW, SUB, nodeHeight } from '@/pipeline/geometry'

export type Kind = 'creds' | 'items' | 'map' | 'ctl'
/** Socket/wire colour by what travels down it, as Blender colours by type. */
const KIND_COLOR: Record<Kind, string> = {
  creds: '#e6c15a', items: '#2fd6a1', map: '#8a84f0', ctl: '#a3a8ae',
}
export interface GSock { label: string; kind: Kind }
export interface GLive { color: string; text: string; active?: boolean }
export interface GNode {
  id: string; title: string; sub: string; head: string
  x: number; y: number
  ins?: GSock[]; outs?: GSock[]
  live?: GLive
  /** Reserve the live row even before there is text, so its arrival does not
   *  change the box's height. */
  liveSlot?: boolean
}
export interface GFrame { id: string; title: string; x: number; y: number; w: number; h: number }
/** 'nodeId.Socket label' on both ends; labels never contain a dot. */
export interface GEdge { from: string; to: string }

export interface NodeGraphHandle {
  fit: () => void
  focus: (id: string) => void
  frame: (id: string) => void
  /** Fit the view to exactly these nodes -- a traced path, say. */
  fitNodes: (ids: Iterable<string>) => void
}

const hasLive = (n: GNode) => !!(n.live || n.liveSlot)
const rowsOf = (n: GNode) => Math.max(n.ins?.length ?? 0, n.outs?.length ?? 0)
const bodyTop = (n: GNode) => n.y + HEAD + SUB + (hasLive(n) ? LIVE : 0)
const heightOf = (n: GNode) => nodeHeight(rowsOf(n), hasLive(n))

function socket(nodes: GNode[], ref: string, side: 'ins' | 'outs') {
  const [id, label] = [ref.slice(0, ref.indexOf('.')), ref.slice(ref.indexOf('.') + 1)]
  const n = nodes.find((m) => m.id === id)
  const i = n?.[side]?.findIndex((s) => s.label === label) ?? -1
  if (!n || i < 0) return null
  return {
    node: n, kind: n[side]![i].kind, label,
    x: n.x + (side === 'outs' ? NODE_W : 0), y: bodyTop(n) + i * ROW + ROW / 2,
  }
}

const MIN_K = 0.1, MAX_K = 2.5
const clamp = (k: number) => Math.min(MAX_K, Math.max(MIN_K, k))

const NodeGraph = forwardRef<NodeGraphHandle, {
  nodes: GNode[]; edges: GEdge[]; frames?: GFrame[]
  /** Size of the drawing itself, in its own units. */
  size: { w: number; h: number }
  /** Height of the viewport it is shown in. */
  viewHeight: number | string
  label: string
  selected?: string | null
  onSelect?: (id: string | null) => void
  /** When set, only these nodes (and the wires between them) stay bright. */
  bright?: ReadonlySet<string> | null
}>(({ nodes, edges, frames = [], size, viewHeight, label, selected, onSelect, bright }, ref) => {
  const [hot, setHot] = useState<string | null>(null)
  const [view, setView] = useState({ x: 0, y: 0, k: 0.5 })
  const [dragging, setDragging] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const touched = useRef(false)
  const drag = useRef({ moved: false, sx: 0, sy: 0, vx: 0, vy: 0 })

  const box = () => {
    const r = wrap.current?.getBoundingClientRect()
    return r && r.width > 0 && r.height > 0 ? { w: r.width, h: r.height } : null
  }

  const fitRect = useCallback((rx: number, ry: number, rw: number, rh: number, pad = 40) => {
    const b = box()
    if (!b) return
    const k = clamp(Math.min((b.w - 2 * pad) / rw, (b.h - 2 * pad) / rh))
    setView({ k, x: (b.w - rw * k) / 2 - rx * k, y: (b.h - rh * k) / 2 - ry * k })
  }, [])

  const fit = useCallback(
    () => fitRect(0, 0, size.w, size.h, 20), [fitRect, size.w, size.h])

  const frameTo = useCallback((id: string) => {
    const f = frames.find((m) => m.id === id)
    if (!f) return
    touched.current = true
    fitRect(f.x, f.y, f.w, f.h, 50)
  }, [frames, fitRect])

  useImperativeHandle(ref, () => ({
    fit: () => { touched.current = true; fit() },
    focus: (id) => {
      const n = nodes.find((m) => m.id === id), b = box()
      if (!n || !b) return
      touched.current = true
      setView((v) => {
        const k = Math.max(v.k, 0.8)
        return { k, x: b.w / 2 - (n.x + NODE_W / 2) * k, y: b.h / 2 - (n.y + heightOf(n) / 2) * k }
      })
    },
    frame: frameTo,
    fitNodes: (ids) => {
      const want = new Set(ids)
      const ns = nodes.filter((n) => want.has(n.id))
      if (!ns.length) return
      const x0 = Math.min(...ns.map((n) => n.x)), y0 = Math.min(...ns.map((n) => n.y))
      const x1 = Math.max(...ns.map((n) => n.x + NODE_W)), y1 = Math.max(...ns.map((n) => n.y + heightOf(n)))
      touched.current = true
      fitRect(x0, y0, x1 - x0, y1 - y0, 50)
    },
  }), [nodes, fit, frameTo, fitRect])

  // Start fitted, and stay fitted on resize until the user has taken over.
  useLayoutEffect(() => { fit() }, [fit])
  useEffect(() => {
    const el = wrap.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => { if (!touched.current) fit() })
    ro.observe(el)
    return () => ro.disconnect()
  }, [fit])

  const zoomAt = useCallback((px: number, py: number, factor: number) => {
    touched.current = true
    setView((v) => {
      const k = clamp(v.k * factor)
      return { k, x: px - (px - v.x) * (k / v.k), y: py - (py - v.y) * (k / v.k) }
    })
  }, [])

  // Native, not React's onWheel: React registers wheel listeners as passive,
  // so preventDefault (which stops the page scrolling under the canvas) would
  // be ignored.
  useEffect(() => {
    const el = wrap.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      touched.current = true
      const r = el.getBoundingClientRect()
      if (e.ctrlKey || e.metaKey) zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.01))
      else setView((v) => ({ ...v, x: v.x - e.deltaX, y: v.y - e.deltaY }))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomAt])

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 || (e.target as HTMLElement).closest('[data-nopan]')) return
    drag.current = { moved: false, sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y }
    setDragging(true)
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - drag.current.sx, dy = ev.clientY - drag.current.sy
      if (Math.abs(dx) + Math.abs(dy) > 4) { drag.current.moved = true; touched.current = true }
      if (drag.current.moved) setView((v) => ({ ...v, x: drag.current.vx + dx, y: drag.current.vy + dy }))
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      setDragging(false)
      // The click that follows pointerup must still see `moved`.
      setTimeout(() => { drag.current.moved = false }, 0)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const select = (id: string | null) => { if (!drag.current.moved) onSelect?.(id) }
  const lit = (id: string) => !bright || bright.has(id)
  const detail = view.k >= 0.5

  return (
    <Box ref={wrap} data-testid="node-graph" onPointerDown={onPointerDown}
         sx={{ position: 'relative', width: '100%', height: viewHeight, overflow: 'hidden',
               bgcolor: '#1b1b1b', touchAction: 'none', cursor: dragging ? 'grabbing' : 'grab',
               userSelect: 'none',
               '& .pulse': { animation: 'pulse 1.1s linear infinite' },
               '@keyframes pulse': { to: { strokeDashoffset: -14 } },
               '@media (prefers-reduced-motion: reduce)': { '& .pulse': { animation: 'none' } },
               '& g[tabindex]:focus': { outline: 'none' },
               '& g[tabindex]:focus-visible rect.frame': { stroke: '#fff', strokeWidth: 1.5 } }}>
      <svg width="100%" height="100%" role="group" aria-label={label} style={{ display: 'block' }}>
        <defs>
          <pattern id="ng-dots" width={20} height={20} patternUnits="userSpaceOnUse"
                   patternTransform={`translate(${view.x},${view.y}) scale(${view.k})`}>
            <circle cx={1} cy={1} r={1} fill="#2c2c2c" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#ng-dots)" onClick={() => select(null)} />
        <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
          {frames.map((f) => {
            // Zoomed out, a fixed 11px title is a speck; scale it so it stays
            // about 12px ON SCREEN -- but never wider than the frame it names.
            const fit = (f.w - 28) / (f.title.length * 0.72)
            // Capped at the header strip (30 units) so it never sits on a box.
            const fs = Math.max(11, Math.min(12 / view.k, fit, 24))
            return (
            <g key={f.id} data-testid={`frame-${f.id}`}>
              <rect x={f.x} y={f.y} width={f.w} height={f.h} rx={10}
                    fill="rgba(255,255,255,0.025)" stroke="#333" strokeWidth={1} />
              <text x={f.x + 14} y={f.y + 3 + fs * 0.85} fontSize={fs} fontWeight={700} letterSpacing={fs * 0.1}
                    fill={view.k < 0.6 ? '#b5b5b5' : '#8d8d8d'} style={{ cursor: 'zoom-in', textTransform: 'uppercase' }}
                    onClick={(e) => { e.stopPropagation(); if (!drag.current.moved) frameTo(f.id) }}>
                <title>Zoom to this section</title>
                {f.title}
              </text>
            </g>
            )
          })}

          {edges.map((e) => {
            const a = socket(nodes, e.from, 'outs'), b = socket(nodes, e.to, 'ins')
            if (!a || !b) return null
            const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5)
            const d = `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`
            const dim = bright
              ? !(bright.has(a.node.id) && bright.has(b.node.id))
              : hot !== null && hot !== a.node.id && hot !== b.node.id
            return (
              <g key={`${e.from}>${e.to}`} opacity={dim ? 0.06 : 1} data-testid="wire">
                <title>{`${a.node.title} → ${b.node.title}: ${a.label}`}</title>
                <path d={d} fill="none" stroke={KIND_COLOR[a.kind]} strokeWidth={2} opacity={0.85} />
                {a.node.live?.active && (
                  <path d={d} className="pulse" fill="none" stroke="#fff" strokeWidth={2.5}
                        strokeLinecap="round" strokeDasharray="0.1 14" />
                )}
              </g>
            )
          })}

          {nodes.map((n) => {
            const h = heightOf(n), top = bodyTop(n) - n.y
            const on = selected === n.id
            return (
              <g key={n.id} transform={`translate(${n.x},${n.y})`} tabIndex={0}
                 role="button" aria-pressed={on} data-testid={`node-${n.id}`}
                 opacity={lit(n.id) ? 1 : 0.2} style={{ cursor: 'pointer' }}
                 onClick={(e) => { e.stopPropagation(); select(on ? null : n.id) }}
                 onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(on ? null : n.id) } }}
                 onMouseEnter={() => setHot(n.id)} onMouseLeave={() => setHot(null)}
                 onFocus={() => setHot(n.id)} onBlur={() => setHot(null)}>
                <title>{`${n.title} — ${n.sub}${n.live ? ` — ${n.live.text}` : ''}`}</title>
                <rect className="frame" width={NODE_W} height={h} rx={4} fill="#2b2b2b"
                      stroke={on ? '#ffffff' : '#0e0e0e'} strokeWidth={on ? 2 : 1} />
                <path d={`M0,4 a4,4 0 0 1 4,-4 h${NODE_W - 8} a4,4 0 0 1 4,4 v${HEAD - 4} h${-NODE_W} z`}
                      fill={n.head} />
                <text x={10} y={15} fontSize={11.5} fontWeight={600} fill="#f2f2f2">{n.title}</text>
                {detail && (
                  <text x={10} y={HEAD + 12} fontSize={9.5} fill="#9b9b9b"
                        fontFamily="ui-monospace, Menlo, Consolas, monospace">{n.sub}</text>
                )}
                {n.live && (
                  <g transform={`translate(0,${HEAD + SUB})`}>
                    <circle cx={14} cy={LIVE / 2} r={3.5} fill={n.live.color} />
                    {detail && (
                      <text x={24} y={LIVE / 2 + 3.5} fontSize={10} fill="#dcdcdc"
                            fontFamily="ui-monospace, Menlo, Consolas, monospace">{n.live.text}</text>
                    )}
                  </g>
                )}
                {(n.ins ?? []).map((s, i) => (
                  <g key={`i${s.label}`}>
                    <circle cx={0} cy={top + i * ROW + ROW / 2} r={4.5} fill={KIND_COLOR[s.kind]} stroke="#0e0e0e" />
                    {detail && <text x={11} y={top + i * ROW + ROW / 2 + 3.5} fontSize={10} fill="#cfcfcf">{s.label}</text>}
                  </g>
                ))}
                {(n.outs ?? []).map((s, i) => (
                  <g key={`o${s.label}`}>
                    <circle cx={NODE_W} cy={top + i * ROW + ROW / 2} r={4.5} fill={KIND_COLOR[s.kind]} stroke="#0e0e0e" />
                    {detail && (
                      <text x={NODE_W - 11} y={top + i * ROW + ROW / 2 + 3.5} fontSize={10} fill="#cfcfcf"
                            textAnchor="end">{s.label}</text>
                    )}
                  </g>
                ))}
              </g>
            )
          })}
        </g>
      </svg>

      <Box data-nopan sx={{ position: 'absolute', top: 8, right: 8, display: 'flex', flexDirection: 'column',
                            gap: 0.5, bgcolor: 'rgba(30,30,30,0.85)', borderRadius: 1, p: 0.25 }}>
        {([['Zoom in', <ZoomInIcon key="i" fontSize="small" />, () => { const b = box(); b && zoomAt(b.w / 2, b.h / 2, 1.25) }],
           ['Zoom out', <ZoomOutIcon key="o" fontSize="small" />, () => { const b = box(); b && zoomAt(b.w / 2, b.h / 2, 0.8) }],
           ['Fit everything', <FitIcon key="f" fontSize="small" />, () => { touched.current = true; fit() }],
        ] as [string, React.ReactNode, () => void][]).map(([t, icon, fn]) => (
          <Tooltip key={t} title={t} placement="left">
            <IconButton size="small" aria-label={t} onClick={fn} sx={{ color: '#ddd' }}>{icon}</IconButton>
          </Tooltip>
        ))}
      </Box>
    </Box>
  )
})
NodeGraph.displayName = 'NodeGraph'

/** What each wire colour carries. */
export const NodeGraphLegend: React.FC = () => (
  <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', rowGap: 1 }}>
    {([['creds', 'credentials'], ['items', 'items'], ['map', 'mapping / ledger rows'],
       ['ctl', 'control']] as [Kind, string][]).map(([k, label]) => (
      <Chip key={k} size="small" variant="outlined" label={label}
            avatar={<Box sx={{ width: 10, height: 10, borderRadius: '50%',
                               bgcolor: KIND_COLOR[k], ml: '8px !important' }} />} />
    ))}
  </Stack>
)

export default NodeGraph
