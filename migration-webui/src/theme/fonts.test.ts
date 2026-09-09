/**
 * src/theme/fonts.test.ts
 * =======================
 * One voice, and only faces the page actually loads.
 *
 * Two things went wrong here, and neither announces itself in a browser --
 * a missing font just silently becomes a different one:
 *
 *   * the base family led with Roboto while every heading led with Google
 *     Sans Flex, so a display heading and the paragraph under it were
 *     visibly two typefaces;
 *   * three pages set "Plus Jakarta Sans" on the wordmark, a face index.html
 *     never requests, so it had been rendering in whatever the system
 *     fallback was since the day it was written.
 */
import { describe, it, expect } from 'vitest'
import { theme as lightTheme, darkTheme } from './index'

// Vite's own raw imports rather than node:fs -- this project's tsconfig has
// no node types, and a test that cannot typecheck is a test that gets
// deleted the next time someone runs tsc.
import html from '../../index.html?raw'

const sources = import.meta.glob('/src/**/*.{ts,tsx}', {
  query: '?raw', import: 'default', eager: true,
}) as Record<string, string>

/** Families index.html actually asks Google Fonts for. */
const loaded = (): string[] =>
  (html.match(/family=([^"&]+)/g) || [])
    .map((x: string) => x.replace(/^family=/, '').split(':')[0].replace(/\+/g, ' '))

/** Every literal font-family string in the app source. */
const declared = (): string[] => {
  const out: string[] = []
  for (const [file, src] of Object.entries(sources)) {
    if (file.includes('.test.')) continue
    for (const m of src.matchAll(/fontFamily:\s*'([^']+)'/g)) out.push(m[1])
  }
  return out
}

describe('the app speaks in one voice', () => {
  it('body text uses the same face as headings', () => {
    const base = lightTheme.typography.fontFamily as string
    const h1 = (lightTheme.typography.h1 as { fontFamily?: string }).fontFamily
    expect(base.split(',')[0]).toBe(h1!.split(',')[0])
  })

  it('and that face is Google Sans Flex', () => {
    expect(lightTheme.typography.fontFamily).toMatch(/^"Google Sans Flex"/)
  })

  it('in dark mode too', () => {
    expect(darkTheme.typography.fontFamily).toBe(lightTheme.typography.fontFamily)
  })

  it('with a real fallback, so a blocked request is not Times', () => {
    const base = lightTheme.typography.fontFamily as string
    expect(base.split(',').length).toBeGreaterThan(2)
    expect(base).toMatch(/sans-serif\s*$/)
  })
})

describe('nothing asks for a face the page never loads', () => {
  it('every declared family is loaded, generic, or monospace', () => {
    const names = loaded()
    const bad = declared().filter((stack) => {
      const first = stack.split(',')[0].replace(/["']/g, '').trim()
      if (/^(ui-monospace|monospace|sans-serif|serif|inherit|-apple-system)$/.test(first))
        return false
      return !names.includes(first)
    })
    expect(bad, `font(s) used but never requested in index.html: ${bad}`).toEqual([])
  })

  it('index.html really does request the brand face', () => {
    expect(loaded()).toContain('Google Sans Flex')
  })
})

describe('code still reads as code', () => {
  it('monospace is left alone', () => {
    /* Logs, ids and payloads need the columns; switching those to the brand
       face to be "consistent" would be consistency at the cost of the one
       place alignment carries meaning. */
    expect(declared().some((s) => s.startsWith('ui-monospace'))).toBe(true)
  })
})

describe('text is crisp on both grounds', () => {
  /* Dark mode read as blurred. Left unset, macOS renders text with subpixel
     antialiasing, which adds weight -- and on a dark ground that extra
     weight looks like a soft, smeared edge. CssBaseline was mounted but the
     theme never overrode it, so this was simply never set. */
  const baseline = (t: typeof lightTheme) =>
    (t.components?.MuiCssBaseline?.styleOverrides ?? {}) as {
      html?: Record<string, string>; body?: Record<string, string>
    }

  it('asks for grayscale antialiasing', () => {
    expect(baseline(lightTheme).html?.WebkitFontSmoothing).toBe('antialiased')
    expect(baseline(lightTheme).html?.MozOsxFontSmoothing).toBe('grayscale')
  })

  it('in dark mode too, which is where it actually mattered', () => {
    expect(baseline(darkTheme).html?.WebkitFontSmoothing).toBe('antialiased')
  })

  it('refuses synthesised weights', () => {
    /* A variable font asked for a weight it lacks gets smeared sideways to
       fake it -- its own kind of blur. Google Sans Flex covers 1..1000, so
       this turns a wrong weight into a visibly wrong weight rather than a
       fuzzy one. */
    expect(baseline(lightTheme).body?.fontSynthesis).toBe('none')
  })
})

describe('surfaces separate in both modes', () => {
  /* default and paper swap roles between the themes: in light, default is
     the tint that lifts a panel off white; in dark, default IS the page and
     paper is the lift. A surface hardcoded to one of them vanishes in the
     other -- which is what happened to the wizard's side panel. */
  it('the two surface tokens are actually different in each mode', () => {
    for (const t of [lightTheme, darkTheme])
      expect(t.palette.background.default).not.toBe(t.palette.background.paper)
  })

  it('and they invert between modes, which is why one token cannot serve both', () => {
    const lighter = (hex: string) =>
      parseInt(hex.slice(1), 16)
    expect(lighter(lightTheme.palette.background.paper))
      .toBeGreaterThan(lighter(lightTheme.palette.background.default))
    expect(lighter(darkTheme.palette.background.paper))
      .toBeGreaterThan(lighter(darkTheme.palette.background.default))
  })
})
