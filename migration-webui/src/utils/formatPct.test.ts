import { describe, it, expect } from 'vitest'
import { formatPct } from './formatPct'

/* Rounding to whole numbers reported the first finished user of two hundred
   as 0%: work completed, progress none, for the seventy-five minutes it
   took the second to land. */

describe('formatPct', () => {
  it('shows the first user of two hundred as something', () => {
    expect(formatPct(0.5)).toBe('0.5%')
  })

  it('shows one item of ten thousand', () => {
    expect(formatPct(0.01)).toBe('0.01%')
  })

  it('still says 0% when genuinely nothing has finished', () => {
    expect(formatPct(0)).toBe('0%')
  })

  it('does not put decimals on a number that moves every few seconds', () => {
    /* 88.24% next to a bar is noise; the decimals only carry information
       while whole numbers are too coarse to show movement. */
    expect(formatPct(88.24)).toBe('88%')
    expect(formatPct(12)).toBe('12%')
  })

  it('drops trailing zeros rather than printing 3.00%', () => {
    expect(formatPct(3)).toBe('3%')
    expect(formatPct(1.5)).toBe('1.5%')
    expect(formatPct(1.507)).toBe('1.51%')
  })

  it('reaches a clean 100', () => {
    expect(formatPct(100)).toBe('100%')
  })

  it('says so rather than printing NaN%', () => {
    expect(formatPct(Number.NaN)).toBe('--')
    expect(formatPct(Number.POSITIVE_INFINITY)).toBe('--')
  })
})
