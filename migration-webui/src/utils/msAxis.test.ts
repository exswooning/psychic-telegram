import { describe, it, expect } from 'vitest'
import { msAxis } from './metricsSeries'

describe('latency axis ticks fit the axis', () => {
  it('reads seconds from a second up, milliseconds below', () => {
    expect(msAxis(4000)).toBe('4 s')
    expect(msAxis(1250)).toBe('1.3 s')
    expect(msAxis(800)).toBe('800ms')
  })
})
