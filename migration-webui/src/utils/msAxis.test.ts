import { describe, it, expect } from 'vitest'
import { msAxis, ratePct } from './metricsSeries'

describe('latency axis ticks fit the axis', () => {
  it('reads seconds from a second up, milliseconds below', () => {
    expect(msAxis(4000)).toBe('4 s')
    expect(msAxis(1250)).toBe('1.3 s')
    expect(msAxis(800)).toBe('800ms')
  })
})

describe('a rate never rounds a real failure away', () => {
  it('shows any count, however small the share', () => {
    expect(ratePct(0, 2)).toBe('<0.1% (2)')
    expect(ratePct(1.5, 30)).toBe('1.5% (30)')
    expect(ratePct(0, 0)).toBe('0%')
  })
})
