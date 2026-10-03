import { describe, it, expect } from 'vitest'
import { darkTheme, theme, tint } from './index'

describe('colours that read in both themes', () => {
  it('a selected nav item sits on a dark tint in dark mode, with light ink', () => {
    // Layout's pill: bgcolor primary.light, color primary.dark
    expect(darkTheme.palette.primary.light).toBe('#22314f')
    expect(darkTheme.palette.primary.dark).toBe('#aecbfa')
    expect(theme.palette.primary.light).toBe('#d2e3fc')        // light mode unchanged
  })

  it('a tinted badge uses the main shade as ink on dark and the dark shade on light', () => {
    expect(tint('success')(darkTheme).color).toBe(darkTheme.palette.success.main)
    expect(tint('success')(theme).color).toBe(theme.palette.success.dark)
  })

  it('anything that is not a palette colour is neutral, never a crash', () => {
    expect(tint('default')(darkTheme).color).toBe(darkTheme.palette.text.secondary)
  })
})
