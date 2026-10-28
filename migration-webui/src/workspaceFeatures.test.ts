import { describe, expect, it } from 'vitest'
import { availableFeatures, findFeatures, workspaceFeatures } from './workspaceFeatures'
import appSource from './App.tsx?raw'

describe('new workspace feature coverage', () => {
  it('keeps every existing page reachable, including safety and settings', () => {
    const routes = [...appSource.matchAll(/<Route path="([^"]+)" element=\{<(\w+)\s*\//g)]
      .filter(([, path, component]) => !path.includes(':') && !['/', '/*', '*', '/login', '/signup', '/pricing'].includes(path) && component !== 'Navigate')
    expect(routes.length).toBeGreaterThan(25)
    for (const [, path] of routes) expect(workspaceFeatures.some(f => f.path === path), path).toBe(true)
  })
  it('uses unique destinations', () => {
    expect(new Set(workspaceFeatures.map(f => f.path)).size).toBe(workspaceFeatures.length)
  })
  it('does not advertise operator or seeding tools to regular accounts', () => {
    expect(availableFeatures(false, false).every(f => !f.operator && !f.seed)).toBe(true)
    expect(availableFeatures(false, true).some(f => f.path === '/seed-wizard')).toBe(true)
    expect(availableFeatures(true, false).some(f => f.path === '/deadman')).toBe(true)
  })
  it('finds capabilities using familiar task names', () => {
    for (const query of ['hybrid email', 'repair', 'sharing', 'domain handover', 'identity', 'transfer methods']) expect(findFeatures(query, true, true).length, query).toBeGreaterThan(0)
    expect(findFeatures('nonsense123', true, true)).toHaveLength(0)
  })
})
