/**
 * Turns the metrics snapshot into rows a chart can draw. Pure, and kept out
 * of the components so the shaping -- which is where a chart quietly lies --
 * has a test that does not depend on a browser laying out an SVG.
 */
import type { LimiterPoint, MetricsSnapshot } from '@/api/controlPlane'

const GB = 1024 ** 3
const two = (n: number) => String(n).padStart(2, '0')

export const clock = (iso: string): string => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso
    : `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`
}

/** Oldest first, whatever order the server sent them in. */
export function historyRows(h: MetricsSnapshot['history'] | undefined) {
  return [...(h ?? [])]
    .sort((a, b) => Date.parse(a.recordedAt) - Date.parse(b.recordedAt))
    .map((p) => ({
      t: clock(p.recordedAt), rps: p.requestsPerSec,
      p95Ms: Math.round(p.p95 * 1000), failures: p.failures,
    }))
}

/** Slowest first: "which call is costing the run" is the question. */
export function operationRows(ops: MetricsSnapshot['operations'] | undefined, n = 12) {
  return [...(ops ?? [])]
    .sort((a, b) => b.p95 - a.p95)
    .slice(0, n)
    .map((o) => ({
      label: o.label, p50Ms: Math.round(o.p50 * 1000), p95Ms: Math.round(o.p95 * 1000),
      calls: o.calls, retries: o.retries, failures: o.failures,
    }))
}

export function limiterRows(l: MetricsSnapshot['limiters'] | undefined) {
  return Object.entries(l ?? {}).sort(([a], [b]) => a.localeCompare(b))
    .map(([name, s]) => ({ name, floor: s.floor, rate: s.rate, ceiling: s.ceiling,
                           rejections: s.rejections, backoffs: s.backoffs }))
}

/** One row per item type, its outcomes side by side. Only FAILED/BLOCKED count
 *  as failures -- SKIPPED_* are decisions, and painting them red is how a clean
 *  run teaches people to ignore red. */
export function volumeRows(v: MetricsSnapshot['volume'] | undefined) {
  const by = new Map<string, { itemType: string; done: number; skipped: number; failed: number }>()
  for (const r of v ?? []) {
    const row = by.get(r.itemType) ?? { itemType: r.itemType, done: 0, skipped: 0, failed: 0 }
    if (r.status === 'SUCCESS') row.done += r.count
    else if (r.status === 'FAILED' || r.status === 'BLOCKED') row.failed += r.count
    else row.skipped += r.count
    by.set(r.itemType, row)
  }
  return [...by.values()].sort((a, b) =>
    (b.done + b.skipped + b.failed) - (a.done + a.skipped + a.failed))
}

export const dayRows = (t: MetricsSnapshot['throughput'] | undefined) =>
  (t?.byDay ?? []).map((d) => ({ day: d.day.slice(5), items: d.items,
                                 gb: Math.round((d.bytes / GB) * 100) / 100 }))

/** Items still to do, from the run's own expected total -- null when it has
 *  none, rather than a bar that claims everything is left. */
export function progressRow(t: MetricsSnapshot['throughput'] | undefined) {
  if (!t || !(t.expectedItems > 0)) return null
  const remaining = Math.min(t.remainingItems, t.expectedItems)
  return [{ name: 'items', done: t.expectedItems - remaining, remaining }]
}

export function transferRow(t: MetricsSnapshot['transfer'] | undefined) {
  if (!t || !(t.dailyCapBytes > 0)) return null
  const used = t.bytesToday / GB, cap = t.dailyCapBytes / GB
  return [{ name: 'today', used: Math.round(used * 100) / 100,
            left: Math.round(Math.max(0, cap - used) * 100) / 100 }]
}

export const clockAt = (epochSec: number) => clock(new Date(epochSec * 1000).toISOString())

export interface SawtoothStat {
  name: string; pushbacks: number
  /** Mean seconds between pushbacks; null with fewer than two. */
  everySec: number | null
  low: number; high: number
}

/**
 * The limiters' rate over time as chart rows, and the numbers that describe
 * the teeth. The controller is additive-increase, multiplicative-decrease, so
 * a healthy run against a real quota looks like a sawtooth: a climb by
 * probes, a sharp drop at each pushback, again and again. A flat line means
 * nothing is pushing back; a steady climb means the ceiling has not been
 * found yet -- and any single reading sits somewhere on a ramp, so a number
 * quoted from one is not a capacity.
 *
 * Rows are one per point, keyed by limiter name (a row only carries the
 * limiter it belongs to; the chart joins across the gaps). A backoff also
 * fills `<name> pushback`, which the chart draws as a dot. History recorded
 * before events existed has only snapshots, so a pushback there is INFERRED
 * from a drop between two snapshots -- and only for a limiter with no events
 * at all, so a real one is never counted twice.
 */
export function sawtoothRows(hist: Record<string, LimiterPoint[]> | undefined) {
  const rows: Record<string, number>[] = []
  const stats: SawtoothStat[] = []
  for (const [name, pts] of Object.entries(hist ?? {})) {
    if (pts.length === 0) continue
    const hasEvents = pts.some((p) => p.kind !== 'sample')
    const dropAt: number[] = []
    let prev: LimiterPoint | undefined
    for (const p of pts) {
      const isDrop = p.kind === 'backoff'
        || (!hasEvents && p.kind === 'sample' && !!prev && p.rate < prev.rate * 0.99)
      const row: Record<string, number> = { ts: p.t, [name]: p.rate }
      if (isDrop) { row[`${name} pushback`] = p.rate; dropAt.push(p.t) }
      rows.push(row)
      prev = p
    }
    const rates = pts.map((p) => p.rate)
    stats.push({
      name, pushbacks: dropAt.length, low: Math.min(...rates), high: Math.max(...rates),
      everySec: dropAt.length >= 2
        ? (dropAt[dropAt.length - 1] - dropAt[0]) / (dropAt.length - 1) : null,
    })
  }
  rows.sort((a, b) => a.ts - b.ts)
  return { rows, stats }
}
