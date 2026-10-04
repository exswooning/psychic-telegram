import { describe, it, expect } from 'vitest'
import { daysUntil, lifecycleLabel } from './lifecycle'

const NOW = Date.parse('2026-10-04T00:00:00Z')

describe('a pair\'s end of life, in one reading', () => {
  it('is "not approved" until someone approves -- nothing approves on its own', () => {
    expect(lifecycleLabel({}, NOW)).toEqual({ label: 'not approved', tone: 'default' })
  })
  it('counts down to the teardown, naming who approved', () => {
    const r = lifecycleLabel({ approved_at: '2026-10-04T00:00:00Z', approved_by: 'boss@x.com',
                               teardown_due_at: '2026-11-03T00:00:00Z' }, NOW)
    expect(r).toEqual({ label: 'teardown in 30 days', tone: 'warning', title: 'approved by boss@x.com' })
    expect(daysUntil('2026-10-01T00:00:00Z', NOW)).toBe(0)
  })
  it('says torn down once it is', () => {
    expect(lifecycleLabel({ torn_down_at: '2026-11-03T00:00:00Z' }, NOW).label).toBe('torn down')
  })
})
