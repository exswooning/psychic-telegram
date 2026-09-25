import { describe, it, expect } from 'vitest'
import { parseSeedRun } from './seedLog'
import {
  fillRateGbPerHour, fillRateRows, fillRows, throttleRows, durationBands, failedServiceRows, itemsAsUsersFinish, slowestUsers,
  storageRows, storageTotals, warningRows,
} from './seedSeries'

const LOG = [
  'Seeding 3 users in x.com at scale \'small\'',
  '  [a@x.com] starting (Eng)',
  '  [a@x.com] done in 100.0s: 10 files, 2 folders',
  '  [b@x.com] done in 50.0s: 5 files (chat failed (HTTP 404))',
  '  [c@x.com] starting (Ops)',
  '  ! chat for b@x.com: HTTP 404 (NOT_FOUND): boom',
  '  ! chat for c@x.com: HTTP 404 (NOT_FOUND): boom',
]
const TOPUP = [
  '  [a@x.com] top-up in 12.3s: 8.1GB -> 9.4GB (3 filler file(s))',
  '  [b@x.com] top-up in 9.0s: 1.0GB -> 1.5GB (1 filler file(s))',
]

describe('a plain seed', () => {
  const run = parseSeedRun(LOG)
  it('accumulates items in finishing order, unfinished users excluded', () => {
    expect(itemsAsUsersFinish(run.users)).toEqual([
      { users: 1, items: 12 }, { users: 2, items: 17 }])
  })
  it('lists slowest first, by local part', () => {
    expect(slowestUsers(run.users)[0]).toEqual({ user: 'a', seconds: 100 })
  })
  it('bands durations, and says nothing for a single user', () => {
    const bands = durationBands(run.users, 2)
    expect(bands.reduce((n, b) => n + b.users, 0)).toBe(2)
    expect(durationBands(run.users.slice(0, 1))).toEqual([])
  })
  it('counts failed services and groups warnings', () => {
    expect(failedServiceRows(run.users)).toEqual([{ service: 'chat', count: 1 }])
    expect(warningRows(run)[0]).toMatchObject({ count: 2 })
  })
  it('has no storage data when nothing filled', () => {
    expect(storageRows(run.users)).toEqual([])
    expect(storageTotals(run.users)).toBeNull()
  })
})

describe('a fill run', () => {
  const run = parseSeedRun([...LOG, ...TOPUP])
  it('reads before and after from the top-up line', () => {
    expect(run.users.find((u) => u.email === 'a@x.com')!.storage)
      .toEqual({ beforeGb: 8.1, afterGb: 9.4 })
  })
  it('ranks by gigabytes added and totals them, with the filler files', () => {
    expect(storageRows(run.users)[0]).toEqual({ user: 'a', before: 8.1, added: 1.3 })
    expect(storageTotals(run.users)).toEqual({ users: 2, addedGb: 1.8, fillers: 4 })
  })
})

describe('fill rate', () => {
  it('is measured from the heartbeats, and absent until there are two', () => {
    expect(fillRateGbPerHour([{ sec: 0, uploadedGb: 0, plannedGb: 9 }])).toBeNull()
    expect(fillRateGbPerHour([{ sec: 0, uploadedGb: 0, plannedGb: 9 },
                              { sec: 1800, uploadedGb: 200, plannedGb: 9 }])).toBe(400)
  })
  it('never reports a negative or undefined rate', () => {
    expect(fillRateGbPerHour(undefined)).toBeNull()
    expect(fillRateGbPerHour([{ sec: 5, uploadedGb: 9, plannedGb: 9 },
                              { sec: 5, uploadedGb: 9, plannedGb: 9 }])).toBeNull()
  })
  it('labels the time axis in minutes', () => {
    expect(fillRows([{ sec: 7020, uploadedGb: 1, plannedGb: 2 }])[0].t).toBe('117m')
  })
})

describe('upload rate between heartbeats', () => {
  it('is per interval, so it moves when the run slows', () => {
    expect(fillRateRows([
      { sec: 0, uploadedGb: 0, plannedGb: 9 }, { sec: 30, uploadedGb: 1.5, plannedGb: 9 },
      { sec: 60, uploadedGb: 2, plannedGb: 9 }]).map((r) => r.gbPerHour)).toEqual([180, 60])
  })
  it('skips an interval with no time or a decrease, rather than plotting nonsense', () => {
    expect(fillRateRows([{ sec: 5, uploadedGb: 3, plannedGb: 9 }, { sec: 5, uploadedGb: 4, plannedGb: 9 },
                         { sec: 35, uploadedGb: 2, plannedGb: 9 }])).toEqual([])
    expect(fillRateRows(undefined)).toEqual([])
  })
})

describe('retries per interval', () => {
  it('turns the cumulative count into what each interval added', () => {
    const r = throttleRows([
      { sec: 30, reqPerSec: 10, retried: 5, retriedPct: 1 },
      { sec: 60, reqPerSec: 9, retried: 5, retriedPct: 0.5 },
      { sec: 90, reqPerSec: 8, retried: 40, retriedPct: 2 }])
    expect(r.map((x) => x.retries)).toEqual([5, 0, 35])
  })
})

