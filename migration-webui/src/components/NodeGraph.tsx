/**
 * A small node-editor renderer: boxes with typed sockets, joined by bezier
 * wires -- the look of Blender's geometry-nodes editor, because "what feeds
 * what" reads faster as wiring than as a list. Pure SVG; a graph library
 * would be a dependency for ~100 lines of geometry.
 *
 * Purely presentational. What the nodes are, and which numbers sit on them,
 * is the caller's business (see pages/Pipeline.tsx) -- nothing here invents
 * a value.
 */
import React, { useState } from 'react'
import { Box, Chip, Stack } from '@mui/material'

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
}
/** 'nodeId.Socket label' on both ends; labels never contain a dot. */
export interface GEdge { from: string; to: string }

const NODE_W = 176
const HEAD = 22, SUB = 16, LIVE = 18, ROW = 18, PAD = 8

const rowsOf = (n: GNode) => Math.max(n.ins?.length ?? 0, n.outs?.length ?? 0)
const bodyTop = (n: GNode) => n.y + HEAD + SUB + (n.live ? LIVE : 0)
const nodeHeight = (n: GNode) =>
  HEAD + SUB + (n.live ? LIVE : 0) + rowsOf(n) * ROW + PAD

function socket(nodes: GNode[], ref: string, side: 'ins' | 'outs') {
  const [id, label] = [ref.slice(0, ref.indexOf('.')), ref.slice(ref.indexOf('.') + 1)]
  const n = nodes.find((m) => m.id === id)
  const i = n?.[side]?.findIndex((s) => s.label === label) ?? -1
  if (!n || i < 0) return null
  return {
    node: n, kind: n[side]![i].kind,
    x: n.x + (side === 'outs' ? NODE_W : 0), y: bodyTop(n) + i * ROW + ROW / 2,
  }
}

const NodeGraph: React.FC<{
  nodes: GNode[]; edges: GEdge[]; width: number; height: number; label: string
}> = ({ nodes, edges, width, height, label }) => {
  const [hot, setHot] = useState<string | null>(null)

  return (
    <Box component="svg" viewBox={`0 0 ${width} ${height}`} role="group" aria-label={label}
         sx={{
           display: 'block', width, maxWidth: 'none', height: 'auto',
           '& .pulse': { animation: 'pulse 1.1s linear infinite' },
           '@keyframes pulse': { to: { strokeDashoffset: -14 } },
           '@media (prefers-reduced-motion: reduce)': { '& .pulse': { animation: 'none' } },
           '& g[tabindex]:focus': { outline: 'none' },
           '& g[tabindex]:focus-visible rect.frame': { stroke: '#fff', strokeWidth: 1.5 },
         }}>
      <defs>
        <pattern id="ng-dots" width="20" height="20" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r="1" fill="#2c2c2c" />
        </pattern>
      </defs>
      <rect width={width} height={height} fill="#1b1b1b" />
      <rect width={width} height={height} fill="url(#ng-dots)" />

      {edges.map((e) => {
        const a = socket(nodes, e.from, 'outs'), b = socket(nodes, e.to, 'ins')
        if (!a || !b) return null
        const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5)
        const d = `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`
        const dim = hot !== null && hot !== a.node.id && hot !== b.node.id
        return (
          <g key={`${e.from}>${e.to}`} opacity={dim ? 0.1 : 1} data-testid="wire">
            <path d={d} fill="none" stroke={KIND_COLOR[a.kind]} strokeWidth={2} opacity={0.85} />
            {a.node.live?.active && (
              <path d={d} className="pulse" fill="none" stroke="#fff" strokeWidth={2.5}
                    strokeLinecap="round" strokeDasharray="0.1 14" />
            )}
          </g>
        )
      })}

      {nodes.map((n) => {
        const h = nodeHeight(n), top = bodyTop(n)
        return (
          <g key={n.id} transform={`translate(${n.x},${n.y})`} tabIndex={0}
             data-testid={`node-${n.id}`}
             onMouseEnter={() => setHot(n.id)} onMouseLeave={() => setHot(null)}
             onFocus={() => setHot(n.id)} onBlur={() => setHot(null)}>
            <title>{`${n.title} — ${n.sub}${n.live ? ` — ${n.live.text}` : ''}`}</title>
            <rect className="frame" width={NODE_W} height={h} rx={4}
                  fill="#2b2b2b" stroke="#0e0e0e" strokeWidth={1} />
            <path d={`M0,4 a4,4 0 0 1 4,-4 h${NODE_W - 8} a4,4 0 0 1 4,4 v${HEAD - 4} h${-NODE_W} z`}
                  fill={n.head} />
            <text x={10} y={15} fontSize={11.5} fontWeight={600} fill="#f2f2f2">{n.title}</text>
            <text x={10} y={HEAD + 12} fontSize={9.5} fill="#9b9b9b"
                  fontFamily="ui-monospace, Menlo, Consolas, monospace">{n.sub}</text>
            {n.live && (
              <g transform={`translate(0,${HEAD + SUB})`}>
                <circle cx={14} cy={LIVE / 2} r={3.5} fill={n.live.color} />
                <text x={24} y={LIVE / 2 + 3.5} fontSize={10} fill="#dcdcdc"
                      fontFamily="ui-monospace, Menlo, Consolas, monospace">{n.live.text}</text>
              </g>
            )}
            {(n.ins ?? []).map((s, i) => (
              <g key={`i${s.label}`}>
                <circle cx={0} cy={top - n.y + i * ROW + ROW / 2} r={4.5}
                        fill={KIND_COLOR[s.kind]} stroke="#0e0e0e" />
                <text x={11} y={top - n.y + i * ROW + ROW / 2 + 3.5} fontSize={10} fill="#cfcfcf">
                  {s.label}
                </text>
              </g>
            ))}
            {(n.outs ?? []).map((s, i) => (
              <g key={`o${s.label}`}>
                <circle cx={NODE_W} cy={top - n.y + i * ROW + ROW / 2} r={4.5}
                        fill={KIND_COLOR[s.kind]} stroke="#0e0e0e" />
                <text x={NODE_W - 11} y={top - n.y + i * ROW + ROW / 2 + 3.5} fontSize={10}
                      fill="#cfcfcf" textAnchor="end">{s.label}</text>
              </g>
            ))}
          </g>
        )
      })}
    </Box>
  )
}

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
