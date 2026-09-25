import { describe, it, expect } from 'vitest'
import { NODE_W, nodeHeight } from './geometry'
import { buildGraph, traceOf } from './layout'
import { EDGES, NODES } from './model'

const g = buildGraph()
const box = (n: (typeof g.nodes)[number]) => {
  const rows = Math.max(n.ins?.length ?? 0, n.outs?.length ?? 0)
  return { x: n.x, y: n.y, w: NODE_W, h: nodeHeight(rows, !!n.liveSlot) }
}
const overlap = (a: { x: number; y: number; w: number; h: number }, b: typeof a) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

describe('layout', () => {
  it('places every node exactly once', () => {
    expect(g.nodes.map((n) => n.id).sort()).toEqual(NODES.map((n) => n.id).sort())
  })

  it('keeps every node inside its own frame', () => {
    for (const n of g.nodes) {
      const f = g.frames.find((fr) => fr.id === g.byId.get(n.id)!.frame)!
      const b = box(n)
      expect(b.x, n.id).toBeGreaterThanOrEqual(f.x)
      expect(b.x + b.w, n.id).toBeLessThanOrEqual(f.x + f.w)
      expect(b.y, n.id).toBeGreaterThanOrEqual(f.y)
      expect(b.y + b.h, n.id).toBeLessThanOrEqual(f.y + f.h)
    }
  })

  it('overlaps nothing: no two nodes, no two frames', () => {
    for (let i = 0; i < g.nodes.length; i++)
      for (let j = i + 1; j < g.nodes.length; j++)
        expect(overlap(box(g.nodes[i]), box(g.nodes[j])), `${g.nodes[i].id} / ${g.nodes[j].id}`).toBe(false)
    for (let i = 0; i < g.frames.length; i++)
      for (let j = i + 1; j < g.frames.length; j++)
        expect(overlap(g.frames[i], g.frames[j]), `${g.frames[i].id} / ${g.frames[j].id}`).toBe(false)
  })

  it('gives every wire a socket at each end, and no node an unused one', () => {
    for (const e of EDGES) {
      expect(g.outs.get(e.from)?.some((s) => s.label === e.label), `${e.from} out ${e.label}`).toBe(true)
      expect(g.ins.get(e.to)?.some((s) => s.label === e.label), `${e.to} in ${e.label}`).toBe(true)
    }
    for (const [id, socks] of g.outs)
      for (const s of socks) expect(EDGES.some((e) => e.from === id && e.label === s.label)).toBe(true)
  })

  it('runs left to right: a wire goes to the right almost everywhere', () => {
    // A handful of tools legitimately write back to the target they sit right
    // of; more than that means a box is in the wrong band.
    const x = new Map(g.nodes.map((n) => [n.id, n.x]))
    const backwards = EDGES.filter((e) => x.get(e.to)! < x.get(e.from)! + NODE_W)
    expect(backwards.length).toBeLessThanOrEqual(18)
  })

  it('reserves the live row for nodes that can show one, so its arrival moves nothing', () => {
    for (const n of g.nodes) expect(n.liveSlot).toBe(!!g.byId.get(n.id)!.live)
  })
})

describe('trace', () => {
  it('follows wires end to end in both directions', () => {
    const t = traceOf(EDGES, 'drive')
    expect(t.up.has('keys')).toBe(true)          // credentials reach Drive through the pool
    expect(t.up.has('src')).toBe(true)
    expect(t.down.has('report')).toBe(true)      // and its results end up in the report
    expect(t.down.has('drive')).toBe(false)      // never includes itself
  })

  it('finds nothing upstream of a starting point and nothing downstream of an end point', () => {
    expect(traceOf(EDGES, 'wizard').up.size).toBe(1)   // only the 2-Step answers feed it
    expect(traceOf(EDGES, 'mc').down.size).toBe(0)
  })
})
