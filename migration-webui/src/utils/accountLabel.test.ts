import { describe, expect, it } from 'vitest'
import { labelFor } from './accountLabel'

const acct = (o: object) => ({ id: 1, email: 'migration-2-4c22631c@migrations.bitport.local', ...o })

describe('labelFor', () => {
  it('names a set-up pair by its domains', () => {
    const a = acct({ source_domain: 'a.x.com', target_domain: 'b.x.com' })
    expect(labelFor(a, [a])).toBe('a.x.com → b.x.com')
  })
  it('names an account whose setup has not run by its own name, not its machine-made login', () => {
    const a = acct({ name: 'a.x.com → b.x.com' })
    expect(labelFor(a, [a])).toBe('a.x.com → b.x.com (setup not run)')
  })
  it('falls back to the login only when there is no name', () => {
    const a = acct({ name: '' })
    expect(labelFor(a, [a])).toBe(a.email)
    const b = acct({ name: a.email })
    expect(labelFor(b, [b])).toBe(b.email)
  })
})
