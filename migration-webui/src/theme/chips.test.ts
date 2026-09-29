import { describe, expect, it } from 'vitest'
import { alpha } from '@mui/material/styles'
import { theme, darkTheme } from './index'

// Google's status chips are tonal (pale tint + dark ink), never solid colour
// with white text -- the solid red "exit 1" pills were the "ugly" complaint.
const chip = (t: typeof theme, ownerState: object) =>
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (t.components!.MuiChip!.styleOverrides!.root as any)({ ownerState, theme: t })

describe('chips follow Google tonal style', () => {
  it('a filled error chip is a pale tint with dark ink, no border', () => {
    const s = chip(theme, { color: 'error', variant: 'filled' })
    expect(s.backgroundColor).toBe(alpha(theme.palette.error.main, 0.12))
    expect(s.color).toBe(theme.palette.error.dark)
    expect(s.border).toBe('none')
  })

  it('an outlined chip keeps its outline, but neutral', () => {
    const s = chip(theme, { color: 'success', variant: 'outlined' })
    expect(s.borderColor).toBe(theme.palette.divider)
    expect(s.color).toBe(theme.palette.success.dark)
  })

  it('dark mode uses the light ink, not the dark one', () => {
    const s = chip(darkTheme, { color: 'error', variant: 'filled' })
    expect(s.color).toBe(darkTheme.palette.error.main)
  })
})
