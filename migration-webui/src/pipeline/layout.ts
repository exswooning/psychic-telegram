/**
 * model -> positioned graph. Nobody places 65 boxes by hand.
 *
 * Sockets are derived from the edges (one per distinct label on each side of a
 * node), so a node's height follows from what it connects to. Frames sit in
 * vertical bands left to right; frames in one band stack; nodes stack inside
 * their column. Every measurement comes from one place, so a test can assert
 * that nothing overlaps.
 */
import type { GEdge, GFrame, GNode, GSock } from '@/components/NodeGraph'
import { NODE_W, nodeHeight } from './geometry'
import { EDGES, FRAMES, NODES, Role } from './model'
import type { PEdge, PFrame, PNode } from './model'

export const COL_GAP = 26
export const NODE_GAP = 14
export const FRAME_PAD = 14
export const FRAME_HEAD = 30
export const BAND_GAP = 70
export const FRAME_GAP = 26
export const MARGIN = 24

/** Header colour by what a node IS, in the palette of a node editor. */
export const ROLE_COLOR: Record<Role, string> = {
  tenant: '#83314a', access: '#7a5f1c', control: '#3b5e3b', engine: '#246283',
  store: '#6a3f8f', verify: '#1f6f6b', guard: '#8f2f2f', ui: '#3c3c8f', seed: '#5b6b2a',
}
export const ROLE_LABEL: Record<Role, string> = {
  tenant: 'Google tenant', access: 'Access & setup', control: 'Control', engine: 'Engine',
  store: 'State', verify: 'Verify & repair', guard: 'Guardrail', ui: 'What you watch', seed: 'Rehearsal & tooling',
}

export interface Graph {
  nodes: GNode[]; frames: GFrame[]; edges: GEdge[]
  width: number; height: number
  byId: Map<string, PNode>
  /** Sockets as derived, for the detail panel. */
  ins: Map<string, GSock[]>; outs: Map<string, GSock[]>
}

export function buildGraph(
  nodes: PNode[] = NODES, edges: PEdge[] = EDGES, frames: PFrame[] = FRAMES,
): Graph {
  const ins = new Map<string, GSock[]>(), outs = new Map<string, GSock[]>()
  const add = (m: Map<string, GSock[]>, id: string, label: string, kind: GSock['kind']) => {
    const list = m.get(id) ?? []
    if (!list.some((s) => s.label === label)) list.push({ label, kind })
    m.set(id, list)
  }
  for (const e of edges) { add(outs, e.from, e.label, e.kind); add(ins, e.to, e.label, e.kind) }

  const heightOf = (n: PNode) =>
    nodeHeight(Math.max(ins.get(n.id)?.length ?? 0, outs.get(n.id)?.length ?? 0), !!n.live)

  // Size each frame from what it holds.
  const sized = frames.map((f) => {
    const members = nodes.filter((n) => n.frame === f.id)
    const cols = members.reduce((c, n) => Math.max(c, n.col + 1), 1)
    const stacks: number[] = Array(cols).fill(0)
    for (const n of members) stacks[n.col] += heightOf(n) + NODE_GAP
    const tallest = Math.max(0, ...stacks.map((s) => s - NODE_GAP))
    return { f, members, cols,
      w: 2 * FRAME_PAD + cols * NODE_W + (cols - 1) * COL_GAP,
      h: FRAME_HEAD + tallest + FRAME_PAD }
  })

  // Bands left to right, frames stacking within a band.
  const bands = [...new Set(frames.map((f) => f.band))].sort((a, b) => a - b)
  const placed: GFrame[] = []
  const gnodes: GNode[] = []
  let x = MARGIN
  let bottom = 0
  for (const b of bands) {
    const inBand = sized.filter((s) => s.f.band === b)
    let y = MARGIN
    for (const s of inBand) {
      placed.push({ id: s.f.id, title: s.f.title, x, y, w: s.w, h: s.h })
      const cursor: number[] = Array(s.cols).fill(FRAME_HEAD)
      for (const n of s.members) {
        const nx = x + FRAME_PAD + n.col * (NODE_W + COL_GAP)
        const ny = y + cursor[n.col]
        cursor[n.col] += heightOf(n) + NODE_GAP
        gnodes.push({
          id: n.id, title: n.title, sub: n.sub, head: ROLE_COLOR[n.role], x: nx, y: ny,
          ins: ins.get(n.id) ?? [], outs: outs.get(n.id) ?? [], liveSlot: !!n.live,
        })
      }
      y += s.h + FRAME_GAP
      bottom = Math.max(bottom, y - FRAME_GAP)
    }
    x += Math.max(...inBand.map((s) => s.w)) + BAND_GAP
  }

  return {
    nodes: gnodes, frames: placed,
    edges: edges.map((e) => ({ from: `${e.from}.${e.label}`, to: `${e.to}.${e.label}` })),
    width: x - BAND_GAP + MARGIN, height: bottom + MARGIN,
    byId: new Map(nodes.map((n) => [n.id, n])), ins, outs,
  }
}

/** Everything upstream and downstream of a node, following wires end to end --
 *  what "where does this come from, and what does it touch" means. */
export function traceOf(edges: PEdge[], id: string) {
  const walk = (next: (e: PEdge) => [string, string]) => {
    const seen = new Set<string>()
    const stack = [id]
    while (stack.length) {
      const cur = stack.pop()!
      for (const e of edges) {
        const [a, b] = next(e)
        if (a === cur && !seen.has(b) && b !== id) { seen.add(b); stack.push(b) }
      }
    }
    return seen
  }
  return {
    up: walk((e) => [e.to, e.from]),
    down: walk((e) => [e.from, e.to]),
  }
}
