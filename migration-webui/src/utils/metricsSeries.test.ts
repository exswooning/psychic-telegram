import { describe, it, expect } from 'vitest'
import {
  clock, dayRows, historyRows, historyStats, limiterRows, operationRows, progressRow,
  sawtoothRows, transferRow, volumeRows, volumeShareRows,
} from './metricsSeries'

describe('the clock', () => {
  // Built from LOCAL hour/minute/second (the Date constructor's own reading of its
  // arguments), then handed to clock() as the ISO string that same instant serialises
  // to -- so the expected wall-clock reading is fixed by the test, never by whatever
  // timezone happens to run it.
  const at = (h: number, m: number, s: number) => new Date(2026, 8, 27, h, m, s).toISOString()

  it('reads a 24-hour hour as 12-hour with AM/PM, not 20:49', () => {
    expect(clock(at(20, 49, 1))).toBe('8:49:01 PM')
  })
  it('reads midnight as 12, not 0', () => {
    expect(clock(at(0, 5, 9))).toBe('12:05:09 AM')
  })
  it('reads noon as 12 PM, not 0 PM', () => {
    expect(clock(at(12, 0, 0))).toBe('12:00:00 PM')
  })
  it('reads a morning hour as AM', () => {
    expect(clock(at(8, 3, 4))).toBe('8:03:04 AM')
  })
  it('still passes through whatever it cannot parse, unchanged', () => {
    expect(clock('not a date')).toBe('not a date')
  })
})

describe('history', () => {
  it('is oldest first whatever order the server sent, with latency in ms', () => {
    const r = historyRows([
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 4, p95: 0.5, failures: 1 },
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 2, p95: 0.25, failures: 0 },
    ])
    expect(r.map((x) => x.rps)).toEqual([2, 4])
    expect(r.map((x) => x.p95Ms)).toEqual([250, 500])
  })
  it('tolerates no history at all', () => expect(historyRows(undefined)).toEqual([]))

  it('shows NEW failures since the last snapshot, not the running total', () => {
    // failures on the wire is metrics.py's cumulative count since the process started --
    // plotted directly under a "per snapshot" title, a burst that stopped hours ago reads
    // as still happening. 5 -> 5 -> 8 -> 8 must show 0, 3, 0, not the raw 5, 5, 8.
    const r = historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 2, p95: 0.25, failures: 5 },
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 2, p95: 0.25, failures: 5 },
      { recordedAt: '2026-09-25T10:00:20Z', requestsPerSec: 2, p95: 0.25, failures: 8 },
    ])
    expect(r.map((x) => x.failures)).toEqual([0, 0, 3])
  })

  it('never goes negative when a counter resets mid-run', () => {
    const r = historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 2, p95: 0.25, failures: 40 },
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 2, p95: 0.25, failures: 2 },
    ])
    expect(r.map((x) => x.failures)).toEqual([0, 0])
  })

  it('reads p50/p99 and their spread in milliseconds, defaulting to 0 when absent', () => {
    const r = historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 2, p95: 0.25, p50: 0.1, p99: 0.9, failures: 0 },
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 2, p95: 0.25, failures: 0 },
    ])
    expect(r[0]).toMatchObject({ p50Ms: 100, p99Ms: 900, spreadMs: 800 })
    expect(r[1]).toMatchObject({ p50Ms: 0, p99Ms: 0, spreadMs: 0 })
  })

  it('rates retries and failures against that snapshot\'s OWN call volume, not a running total', () => {
    const r = historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 2, p95: 0.25, calls: 100, retries: 5, failures: 2 },
      // +100 calls, +5 retries (of those 100), +8 failures (of those 100)
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 2, p95: 0.25, calls: 200, retries: 10, failures: 10 },
    ])
    expect(r[1]).toMatchObject({ callsDelta: 100, retriesDelta: 5, retryRatePct: 5, failureRatePct: 8 })
  })

  it('rates as 0 rather than dividing by zero when a snapshot made no new calls', () => {
    const r = historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 0, p95: 0, calls: 50, retries: 1, failures: 0 },
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 0, p95: 0, calls: 50, retries: 1, failures: 0 },
    ])
    expect(r[1]).toMatchObject({ callsDelta: 0, retryRatePct: 0, failureRatePct: 0 })
  })

  it('averages rps over a trailing window instead of the raw, jumpy series', () => {
    const at = (s: number, rps: number) =>
      ({ recordedAt: `2026-09-25T10:00:${String(s).padStart(2, '0')}Z`, requestsPerSec: rps, p95: 0, failures: 0 })
    const r = historyRows([at(0, 10), at(10, 20), at(20, 0)], 2)   // window of 2
    expect(r.map((x) => x.rpsAvg)).toEqual([10, 15, 10])   // [10], [10,20]->15, [20,0]->10
  })
})

describe('historyStats', () => {
  it('is null without any history rather than a chart of zeroes', () => {
    expect(historyStats(historyRows(undefined))).toBeNull()
  })

  it('reports mean, spread and coefficient of variation for rps and p95', () => {
    const rows = historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 100, p95: 0.1, failures: 0 },
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 300, p95: 0.3, failures: 0 },
    ])
    const s = historyStats(rows)!
    expect(s.rps.mean).toBe(200)
    expect(s.rps.min).toBe(100)
    expect(s.rps.max).toBe(300)
    expect(s.rps.cvPct).toBeGreaterThan(0)   // varied, not flat
    expect(s.p95Ms.mean).toBe(200)
  })

  it('has no spread to report from a single snapshot -- zero, not NaN', () => {
    const s = historyStats(historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 50, p95: 0.1, failures: 0 },
    ]))!
    expect(s.rps.stdev).toBe(0)
    expect(s.rps.cvPct).toBe(0)
  })

  it('combines the window\'s retry/failure rate as total-over-total, not an average of per-row percentages', () => {
    // The first row only ever sets a baseline (its own delta is 0, same as `failures`
    // elsewhere). Row 2: 1 retry of 10 new calls (10%). Row 3: 1 retry of 1,000 new
    // calls (~0.1%). A naive average of those two percentages is ~5%; the combined
    // (total retries / total calls) rate is 2/1010, not close to either one alone.
    const s = historyStats(historyRows([
      { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 0, p95: 0, calls: 0, retries: 0, failures: 0 },
      { recordedAt: '2026-09-25T10:00:10Z', requestsPerSec: 0, p95: 0, calls: 10, retries: 1, failures: 0 },
      { recordedAt: '2026-09-25T10:00:20Z', requestsPerSec: 0, p95: 0, calls: 1010, retries: 2, failures: 0 },
    ]))!
    expect(s.totalCalls).toBe(1010)
    expect(s.totalRetries).toBe(2)
    expect(s.retryRatePct).toBe(0.2)
  })
})

describe('operations gain a per-call rate alongside the raw counts', () => {
  it('rates retries/failures against that operation\'s own calls', () => {
    const r = operationRows([
      { label: 'drive.files.create', calls: 1000, retries: 50, failures: 10, p50: 0.1, p95: 0.2 },
      { label: 'drive.permissions.create', calls: 10, retries: 5, failures: 0, p50: 0.05, p95: 0.1 },
    ])
    expect(r.find((o) => o.label === 'drive.files.create')).toMatchObject({ retryPct: 5, failurePct: 1 })
    expect(r.find((o) => o.label === 'drive.permissions.create')).toMatchObject({ retryPct: 50, failurePct: 0 })
  })

  it('rates as 0 rather than dividing by zero for an operation with no calls', () => {
    const r = operationRows([{ label: 'unused', calls: 0, retries: 0, failures: 0, p50: 0, p95: 0 }])
    expect(r[0]).toMatchObject({ retryPct: 0, failurePct: 0 })
  })
})

describe('volumeShareRows', () => {
  it('reshapes counts into each type\'s own share, so a small and a huge type are comparable', () => {
    const r = volumeShareRows([
      { itemType: 'file', status: 'SUCCESS', count: 90 },
      { itemType: 'file', status: 'FAILED', count: 10 },
      { itemType: 'event', status: 'SUCCESS', count: 2 },
      { itemType: 'event', status: 'FAILED', count: 2 },
    ])
    expect(r.find((x) => x.itemType === 'file')).toEqual({ itemType: 'file', done: 90, skipped: 0, failed: 10 })
    // Half the events failed -- the same SHAPE as a much larger corpus with the same split.
    expect(r.find((x) => x.itemType === 'event')).toEqual({ itemType: 'event', done: 50, skipped: 0, failed: 50 })
  })

  it('is 0/0/0 rather than NaN for a type with no items at all', () => {
    expect(volumeShareRows([])).toEqual([])
  })
})

describe('operations', () => {
  it('lists the slowest first and caps the list', () => {
    const ops = Array.from({ length: 20 }, (_, i) => (
      { label: `op${i}`, calls: 1, retries: 0, failures: 0, p50: i / 100, p95: i / 10 }))
    const r = operationRows(ops, 5)
    expect(r).toHaveLength(5)
    expect(r[0].label).toBe('op19')
    expect(r[0].p95Ms).toBe(1900)
  })
})

describe('volume', () => {
  it('counts only FAILED and BLOCKED as failures; skips are not red', () => {
    const r = volumeRows([
      { itemType: 'file', status: 'SUCCESS', count: 90 },
      { itemType: 'file', status: 'SKIPPED_TOO_LARGE', count: 7 },
      { itemType: 'file', status: 'FAILED', count: 2 },
      { itemType: 'file', status: 'BLOCKED', count: 1 },
      { itemType: 'event', status: 'SUCCESS', count: 5 },
    ])
    expect(r[0]).toEqual({ itemType: 'file', done: 90, skipped: 7, failed: 3 })
    expect(r[1].itemType).toBe('event')
  })
})

describe('progress and transfer say nothing rather than guess', () => {
  it('has no progress bar without an expected total', () => {
    expect(progressRow({ expectedItems: 0, remainingItems: 0 } as never)).toBeNull()
    expect(progressRow(undefined)).toBeNull()
  })
  it('splits done and remaining, never remaining above the total', () => {
    expect(progressRow({ expectedItems: 100, remainingItems: 30 } as never)![0])
      .toMatchObject({ done: 70, remaining: 30 })
    expect(progressRow({ expectedItems: 100, remainingItems: 500 } as never)![0])
      .toMatchObject({ done: 0, remaining: 100 })
  })
  it('has no cap bar when no cap is set, and does not go negative over it', () => {
    expect(transferRow({ bytesToday: 5, dailyCapBytes: 0 })).toBeNull()
    const gb = 1024 ** 3
    expect(transferRow({ bytesToday: 12 * gb, dailyCapBytes: 10 * gb })![0])
      .toMatchObject({ used: 12, left: 0 })
  })
})

describe('days and limiters', () => {
  it('reports gigabytes, not bytes', () => {
    expect(dayRows({ byDay: [{ day: '2026-09-25', items: 3, bytes: 1024 ** 3 }] } as never))
      .toEqual([{ day: '09-25', items: 3, gb: 1 }])
  })
  it('sorts limiters by name', () => {
    const s = { rate: 1, floor: 0, ceiling: 2, rejections: 0, backoffs: 0 }
    expect(limiterRows({ drive: s, chat: s }).map((x) => x.name)).toEqual(['chat', 'drive'])
  })
})

describe('the limiter sawtooth', () => {
  const P = (t: number, rate: number, kind: 'probe' | 'backoff' | 'sample') => ({ t, rate, kind })

  it('counts pushbacks and the spacing between them from the limiter\'s own events', () => {
    const { stats, rows } = sawtoothRows({ target: [
      P(0, 40, 'probe'), P(20, 44, 'probe'), P(40, 30, 'backoff'),
      P(60, 33, 'probe'), P(140, 23, 'backoff'), P(200, 25, 'probe')] })
    expect(stats[0]).toMatchObject({ name: 'target', pushbacks: 2, everySec: 100, low: 23, high: 44 })
    // A pushback also fills its own key, so the chart can mark it.
    expect(rows.filter((r) => 'target pushback' in r).map((r) => r.ts)).toEqual([40, 140])
  })

  it('infers pushbacks from drops between snapshots only when there are no events', () => {
    const { stats } = sawtoothRows({ target: [
      P(0, 50, 'sample'), P(15, 60, 'sample'), P(30, 42, 'sample'), P(45, 47, 'sample')] })
    expect(stats[0].pushbacks).toBe(1)                     // 60 -> 42
    // With real events present, a snapshot lower than the one before it is
    // not a second pushback -- the event already said so.
    const both = sawtoothRows({ target: [
      P(0, 60, 'sample'), P(10, 42, 'backoff'), P(15, 42, 'sample')] })
    expect(both.stats[0].pushbacks).toBe(1)
  })

  it('has no spacing to report for fewer than two pushbacks', () => {
    expect(sawtoothRows({ t: [P(0, 10, 'backoff')] }).stats[0].everySec).toBeNull()
  })

  it('orders every limiter\'s points by time, each row carrying only its own limiter', () => {
    const { rows } = sawtoothRows({ source: [P(5, 1200, 'sample')], target: [P(1, 40, 'probe')] })
    expect(rows.map((r) => r.ts)).toEqual([1, 5])
    expect(rows[0]).toEqual({ ts: 1, target: 40 })
  })

  it('is empty without history, rather than a line at zero', () => {
    expect(sawtoothRows(undefined)).toEqual({ rows: [], stats: [] })
    expect(sawtoothRows({ target: [] }).rows).toEqual([])
  })
})
