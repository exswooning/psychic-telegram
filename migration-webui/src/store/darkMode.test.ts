import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { initialDarkMode, useMigrationStore } from './index'

const memory = () => {
  const m = new Map<string, string>()
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => { m.set(k, v) },
           removeItem: (k: string) => { m.delete(k) }, clear: () => m.clear() }
}

describe('dark mode survives a reload', () => {
  beforeEach(() => { vi.stubGlobal('localStorage', memory()) })
  afterEach(() => { vi.unstubAllGlobals() })

  it('remembers the choice', () => {
    const before = useMigrationStore.getState().darkMode
    useMigrationStore.getState().toggleDarkMode()
    expect(localStorage.getItem('bitport.darkMode')).toBe(String(!before))
    expect(initialDarkMode()).toBe(!before)
  })

  it('follows the system until something is chosen', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('dark') }))
    expect(initialDarkMode()).toBe(true)
    localStorage.setItem('bitport.darkMode', 'false')
    expect(initialDarkMode()).toBe(false)
  })

  it('a browser that blocks storage still opens, in the system setting', () => {
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('blocked') } })
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    expect(initialDarkMode()).toBe(false)
  })
})
