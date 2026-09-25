/**
 * The picture is data, so it can be checked like data. The point of most of
 * these is drift: the graph names files and describes behaviour, and a
 * description that has stopped being true is worse than none.
 */
import { describe, it, expect } from 'vitest'
import { EDGES, FRAMES, NODES } from './model'

// The repo's files, listed at build time (no Node fs, so no extra typings).
// Widen this if a node ever cites a file somewhere else.
const REPO = [
  ...Object.keys(import.meta.glob(['../../../*.py', '../../../Caddyfile', '../../../data-generator/*.py']))
    .map((p) => p.replace('../../../', '')),
  // Inside this project, so globbed from here rather than via the repo root.
  ...Object.keys(import.meta.glob('../pages/*.tsx'))
    .map((p) => p.replace('../pages/', 'migration-webui/src/pages/')),
]

const ids = new Set(NODES.map((n) => n.id))

describe('the system model', () => {
  it('has unique node ids, and ids that cannot break a wire reference', () => {
    expect(ids.size).toBe(NODES.length)
    // Wires are addressed as "id.Label", split at the first dot.
    for (const n of NODES) expect(n.id).not.toContain('.')
  })

  it('puts every node in a real frame, and gives every frame something', () => {
    const frames = new Set(FRAMES.map((f) => f.id))
    for (const n of NODES) expect(frames.has(n.frame), `${n.id} -> ${n.frame}`).toBe(true)
    for (const f of FRAMES) expect(NODES.some((n) => n.frame === f.id), f.id).toBe(true)
  })

  it('joins only nodes that exist', () => {
    for (const e of EDGES) {
      expect(ids.has(e.from), `${e.from} -> ${e.to}`).toBe(true)
      expect(ids.has(e.to), `${e.from} -> ${e.to}`).toBe(true)
      expect(e.from).not.toBe(e.to)
    }
  })

  it('has no duplicate wire, and no wire that could be read two ways', () => {
    const seen = new Set<string>()
    for (const e of EDGES) {
      const k = `${e.from}>${e.to}:${e.label}`
      expect(seen.has(k), k).toBe(false)
      seen.add(k)
    }
  })

  it('names only files that exist in the repo', () => {
    expect(REPO.length).toBeGreaterThan(50)        // the glob found the repo at all
    const missing = NODES.flatMap((n) => n.files.filter((f) => !REPO.includes(f)).map((f) => `${n.id}: ${f}`))
    expect(missing).toEqual([])
  })

  it('explains every part, in text that fits its box', () => {
    for (const n of NODES) {
      expect(n.about.length, `${n.id} about`).toBeGreaterThan(20)
      expect(n.title.length, `${n.id} title`).toBeLessThanOrEqual(24)
      expect(n.sub.length, `${n.id} sub`).toBeLessThanOrEqual(27)
    }
  })

  it('points pages only at routes the app has', () => {
    const routes = ['/wizard', '/wizard?mode=seed', '/scope', '/identities', '/settings', '/jobs', '/nodes',
                    '/services', '/verification', '/errors', '/report', '/mission-control', '/metrics',
                    '/activity', '/tests']
    for (const n of NODES) if (n.page) expect(routes, `${n.id} -> ${n.page}`).toContain(n.page)
  })

  it('only asks for live counts from stages the ledger reports', () => {
    const known = ['discovery', 'authentication', 'user_creation', 'gmail', 'drive', 'calendar',
                   'contacts', 'chat', 'permissions', 'validation', 'report']
    for (const n of NODES) if (n.stage) expect(known, n.id).toContain(n.stage)
  })
})
