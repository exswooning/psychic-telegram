import { describe, it, expect } from 'vitest'
import {
  dayRows, historyRows, limiterRows, operationRows, progressRow, sawtoothRows, transferRow,
  volumeRows,
} from './metricsSeries'

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
