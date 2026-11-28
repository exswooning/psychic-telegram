/**
 * An icon button with no text is announced as just "button". MUI's Tooltip names the control
 * it wraps directly, but not one inside a <span> (the usual way to tooltip a disabled button) --
 * five "Re-check" buttons and two close buttons shipped that way. Every IconButton must carry
 * an aria-label, or sit straight inside a Tooltip.
 */
import { describe, it, expect } from 'vitest'

// Every page and component as text; tests and this folder are left out.
const sources = import.meta.glob(['../**/*.tsx', '!../**/*.test.tsx'], {
  query: '?raw', import: 'default', eager: true,
}) as Record<string, string>

/** The `>` that closes a JSX opening tag, skipping braces, quotes and the `=>` of arrow functions. */
const tagEnd = (s: string, from: number): number => {
  let depth = 0
  let quote = ''
  for (let i = from; i < s.length; i++) {
    const c = s[i]
    if (quote) { if (c === quote) quote = ''; continue }
    if ('"\'`'.includes(c)) quote = c
    else if (c === '{') depth++
    else if (c === '}') depth--
    else if (c === '>' && depth === 0 && s[i - 1] !== '=') return i
  }
  return s.length
}

describe('every icon button has a name', () => {
  it('has an aria-label, or sits directly inside a Tooltip', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)      // the glob found the app
    const bad: string[] = []
    for (const [file, s] of Object.entries(sources)) {
      for (let i = s.indexOf('<IconButton'); i >= 0; i = s.indexOf('<IconButton', i + 1)) {
        const tag = s.slice(i, tagEnd(s, i + 11))
        if (/aria-label(ledby)?=/.test(tag)) continue
        if (/<Tooltip\b[^<]*>\s*$/.test(s.slice(Math.max(0, i - 300), i))) continue
        bad.push(`${file}:${s.slice(0, i).split('\n').length}`)
      }
    }
    expect(bad).toEqual([])
  })

  it('gives every switch and checkbox a name: an aria-label, or a FormControlLabel around it', () => {
    const bad: string[] = []
    for (const [file, s] of Object.entries(sources)) {
      for (const tagName of ['<Switch', '<Checkbox']) {
        for (let i = s.indexOf(tagName); i >= 0; i = s.indexOf(tagName, i + 1)) {
          if (!/[\s/>]/.test(s[i + tagName.length] ?? ' ')) continue          // <SwitchFoo, not <Switch
          const tag = s.slice(i, tagEnd(s, i + tagName.length))
          if (/aria-label(ledby)?=|aria-label'/.test(tag)) continue
          if (/control=\{\s*$/.test(s.slice(Math.max(0, i - 80), i))) continue
          bad.push(`${file}:${s.slice(0, i).split('\n').length}`)
        }
      }
    }
    expect(bad).toEqual([])
  })

  it('never uses a placeholder as the only name of a text field', () => {
    const bad: string[] = []
    for (const [file, s] of Object.entries(sources)) {
      for (let i = s.indexOf('<TextField'); i >= 0; i = s.indexOf('<TextField', i + 1)) {
        const tag = s.slice(i, tagEnd(s, i + 10))
        if (!/\splaceholder=/.test(tag)) continue
        if (/\slabel=|aria-label/.test(tag)) continue
        bad.push(`${file}:${s.slice(0, i).split('\n').length}`)
      }
    }
    expect(bad).toEqual([])
  })
})
