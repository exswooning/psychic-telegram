/**
 * The wizard puts the tenant's domain in its display heading, and a fixed
 * 3.25rem broke source.rohitrokaya.com.np across two lines mid-word --
 * "source.rohitrokaya.co" / "m.np". A domain is one token; there is no
 * correct place to break one.
 *
 * jsdom reports every layout dimension as 0, so the measuring half cannot
 * be exercised here. The arithmetic is the part that decides the outcome,
 * and it is pure precisely so it can be.
 */
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import FitHeading, { fitFontRem } from './FitHeading'

const MAX = 3.25
const MIN = 1.375

describe('fitFontRem', () => {
  it('leaves a short heading at the design size', () => {
    expect(fitFontRem(560, 300, MAX, MIN)).toBe(MAX)
  })

  it('leaves one that exactly fits alone', () => {
    expect(fitFontRem(560, 560, MAX, MIN)).toBe(MAX)
  })

  it('shrinks one that overflows, in proportion', () => {
    // Twice too wide -> half the size.
    expect(fitFontRem(280, 560, MAX, MIN)).toBeCloseTo(MAX / 2, 5)
  })

  it('shrinks the real case enough to fit', () => {
    /* source.rohitrokaya.com.np measured ~700px at 3.25rem in a 560px
       column -- the exact string that wrapped. */
    const rem = fitFontRem(560, 700, MAX, MIN)
    expect(rem).toBeLessThan(MAX)
    expect(700 * (rem / MAX)).toBeLessThanOrEqual(560 + 0.001)
  })

  it('never goes below the floor, however long the name', () => {
    /* Past some length, shrinking further stops helping and starts being
       unreadable. Better to clip than to render 4px type. */
    expect(fitFontRem(560, 99999, MAX, MIN)).toBe(MIN)
  })

  it('holds the design size when nothing is measurable yet', () => {
    /* First paint, display:none, and jsdom all report 0. Collapsing to the
       minimum there would flash small and then grow. */
    expect(fitFontRem(0, 0, MAX, MIN)).toBe(MAX)
    expect(fitFontRem(560, 0, MAX, MIN)).toBe(MAX)
    expect(fitFontRem(0, 700, MAX, MIN)).toBe(MAX)
  })
})

describe('the element itself', () => {
  it('renders the text', () => {
    render(<FitHeading text="source.rohitrokaya.com.np" />)
    expect(screen.getByText('source.rohitrokaya.com.np')).toBeInTheDocument()
  })

  it('is a real heading, not a styled div', () => {
    render(<FitHeading text="acme.com" />)
    expect(screen.getByRole('heading', { name: 'acme.com' })).toBeInTheDocument()
  })

  it('refuses to wrap, which is what makes the measurement mean anything', () => {
    /* Allowed to wrap, scrollWidth reports the WRAPPED width and every
       heading "fits" -- the measurement would silently always pass. */
    render(<FitHeading text="source.rohitrokaya.com.np" />)
    const el = screen.getByRole('heading')
    expect(getComputedStyle(el).whiteSpace).toBe('nowrap')
  })
})
