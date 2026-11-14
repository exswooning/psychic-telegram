import { describe, it, expect } from 'vitest'
import { plainRunDetail } from './runDetail'

describe('a finished run reads as a sentence', () => {
  const repair = JSON.stringify({
    user: 'finance@s.example', services: ['drive', 'gmail', 'calendar'], state: 'NEEDS_ATTENTION',
    repairs: { directory: { candidates: 0, changed: 0, errors: [] }, drive: { candidates: 2, changed: 2, errors: ['x'] } },
  })

  it('turns a repair summary into who, state, scope and what changed', () => {
    expect(plainRunDetail(repair)).toBe('finance@s.example · needs attention · 3 services · 2 fixed, 1 error')
  })
  it('leaves ordinary text, empty values and unfamiliar JSON alone', () => {
    expect(plainRunDetail('exit 0')).toBe('exit 0')
    expect(plainRunDetail(null)).toBe('')
    expect(plainRunDetail('{"foo": 1}')).toBe('{"foo": 1}')
    expect(plainRunDetail('{not json')).toBe('{not json')
  })
})
