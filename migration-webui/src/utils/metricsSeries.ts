/**
 * Turns the metrics snapshot into rows a chart can draw. Pure, and kept out
 * of the components so the shaping -- which is where a chart quietly lies --
 * has a test that does not depend on a browser laying out an SVG.
 */
import type { LimiterPoint, MetricsSnapshot, MigrationFailure } from '@/api/controlPlane'

const GB = 1024 ** 3
const two = (n: number) => String(n).padStart(2, '0')
const round1 = (n: number) => Math.round(n * 10) / 10
const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length
const stdev = (xs: number[], m: number) => Math.sqrt(mean(xs.map((x) => (x - m) ** 2)))

/** 12-hour, the reading every chart axis and "last checked" timestamp in this app now
 *  shares -- a 24-hour "20:49:01" reads as 8:49 AM to more than half the people who might
 *  open this dashboard, and a chart nobody can read at a glance is not much of a chart. */
export const clock = (iso: string): string => {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const h24 = d.getHours()
  const h12 = h24 % 12 || 12
  const ampm = h24 < 12 ? 'AM' : 'PM'
  return `${h12}:${two(d.getMinutes())}:${two(d.getSeconds())} ${ampm}`
}

/** Oldest first, whatever order the server sent them in.
 *
 * `failures` on the wire is metrics.py's own running total since the process started (or
 * its last reset), the same number "Retries and failures by operation" already shows as a
 * lifetime total -- correct there, but this chart is titled "per snapshot", and plotting
 * the cumulative count directly under that title drew a flat plateau at whatever height a
 * burst hours earlier left it, which reads as failures happening continuously, right now,
 * long after they stopped. Diffed against the previous row instead, clamped at 0 so a
 * counter reset (a fresh process picking up mid-run) reads as "unknown", never negative.
 * `calls` and `retries` get the identical treatment (`callsDelta`/`retriesDelta`) so a rate
 * -- retries or failures as a % of calls actually made in that snapshot -- can be plotted
 * instead of a raw count that means something different at 50 calls/s than at 1,200.
 * `rpsAvg` is a trailing 5-point mean of `rps`: the raw series is one Google response away
 * from its neighbour, which is real but not the question "is this run speeding up or
 * slowing down" -- the average answers that, without inventing a fake tenth reading. */
export function historyRows(h: MetricsSnapshot['history'] | undefined, avgWindow = 5) {
  const ordered = [...(h ?? [])]
    .sort((a, b) => Date.parse(a.recordedAt) - Date.parse(b.recordedAt))
  let prevFailures: number | null = null
  let prevCalls: number | null = null
  let prevRetries: number | null = null
  const rows = ordered.map((p) => {
    const failures = prevFailures === null ? 0 : Math.max(0, p.failures - prevFailures)
    prevFailures = p.failures
    const calls = p.calls
    const callsDelta = prevCalls === null || calls === undefined ? 0 : Math.max(0, calls - prevCalls)
    if (calls !== undefined) prevCalls = calls
    const retries = p.retries
    const retriesDelta = prevRetries === null || retries === undefined ? 0 : Math.max(0, retries - prevRetries)
    if (retries !== undefined) prevRetries = retries
    const p50Ms = Math.round((p.p50 ?? 0) * 1000)
    const p99Ms = Math.round((p.p99 ?? 0) * 1000)
    return {
      t: clock(p.recordedAt), rps: p.requestsPerSec,
      p50Ms, p95Ms: Math.round(p.p95 * 1000), p99Ms,
      spreadMs: Math.max(0, p99Ms - p50Ms),
      failures, callsDelta, retriesDelta,
      retryRatePct: callsDelta > 0 ? round1((retriesDelta / callsDelta) * 100) : 0,
      failureRatePct: callsDelta > 0 ? round1((failures / callsDelta) * 100) : 0,
    }
  })
  return rows.map((r, i) => ({
    ...r,
    rpsAvg: round1(mean(rows.slice(Math.max(0, i - avgWindow + 1), i + 1).map((x) => x.rps))),
  }))
}

export interface HistoryStats {
  /** cvPct = coefficient of variation as a percentage (stdev / mean * 100), null when
   *  mean is 0 -- a ratio of two things that are both zero is not "perfectly stable". */
  rps: { mean: number; stdev: number; min: number; max: number; cvPct: number | null }
  p95Ms: { mean: number; stdev: number }
  totalCalls: number; totalRetries: number; totalFailures: number
  retryRatePct: number; failureRatePct: number
}

/**
 * Whether the run's own rate held steady or lurched, as numbers rather than a chart
 * someone has to eyeball: mean, spread (stdev) and coefficient of variation (stdev / mean,
 * unitless and so comparable across runs at very different rates) for requests/sec and p95
 * latency over the current window. The window's retry/failure rate is the combined
 * total-over-total (sum of every row's own delta), not an average of per-row percentages --
 * averaging percentages from snapshots with wildly different call volumes weights a quiet
 * snapshot the same as a busy one, which is exactly backwards.
 */
export function historyStats(rows: ReturnType<typeof historyRows>): HistoryStats | null {
  if (rows.length === 0) return null
  const rpsVals = rows.map((r) => r.rps)
  const p95Vals = rows.map((r) => r.p95Ms)
  const rpsMean = mean(rpsVals)
  const p95Mean = mean(p95Vals)
  const totalCalls = rows.reduce((a, r) => a + r.callsDelta, 0)
  const totalRetries = rows.reduce((a, r) => a + r.retriesDelta, 0)
  const totalFailures = rows.reduce((a, r) => a + r.failures, 0)
  return {
    rps: {
      mean: round1(rpsMean), stdev: round1(stdev(rpsVals, rpsMean)),
      min: Math.min(...rpsVals), max: Math.max(...rpsVals),
      cvPct: rpsMean > 0 ? round1((stdev(rpsVals, rpsMean) / rpsMean) * 100) : null,
    },
    p95Ms: { mean: Math.round(p95Mean), stdev: Math.round(stdev(p95Vals, p95Mean)) },
    totalCalls, totalRetries, totalFailures,
    retryRatePct: totalCalls > 0 ? round1((totalRetries / totalCalls) * 100) : 0,
    failureRatePct: totalCalls > 0 ? round1((totalFailures / totalCalls) * 100) : 0,
  }
}

/** Slowest first: "which call is costing the run" is the question. `retryPct`/`failurePct`
 *  are retries/failures as a share of that operation's OWN calls -- five retries out of ten
 *  calls and five out of ten thousand are not the same fact, and the raw counts alone read
 *  as though they were. */
export function operationRows(ops: MetricsSnapshot['operations'] | undefined, n = 12) {
  return [...(ops ?? [])]
    .sort((a, b) => b.p95 - a.p95)
    .slice(0, n)
    .map((o) => ({
      label: o.label, p50Ms: Math.round(o.p50 * 1000), p95Ms: Math.round(o.p95 * 1000),
      calls: o.calls, retries: o.retries, failures: o.failures,
      retryPct: o.calls > 0 ? round1((o.retries / o.calls) * 100) : 0,
      failurePct: o.calls > 0 ? round1((o.failures / o.calls) * 100) : 0,
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

/** volumeRows, reshaped as each type's own share (0-100) instead of a raw count. A
 *  type with 40 items and one with 40,000 cannot be compared by outcome SHAPE on the raw
 *  chart -- the small one is invisible beside the large one even if it failed entirely. */
export function volumeShareRows(v: MetricsSnapshot['volume'] | undefined) {
  return volumeRows(v).map((r) => {
    const total = r.done + r.skipped + r.failed
    const pct = (x: number) => (total > 0 ? round1((x / total) * 100) : 0)
    return { itemType: r.itemType, done: pct(r.done), skipped: pct(r.skipped), failed: pct(r.failed) }
  })
}

export interface FailureCauseRow { name: string; value: number; reason: string; itemType: string }

/**
 * Failures already grouped by cause on the server (ids and URLs stripped, so the same
 * underlying error from a thousand different files is one slice, not a thousand) --
 * capped here to the busiest `topN` plus a single "other" slice. A pie is read as a
 * picture, at a glance; twenty-five wedges is not a picture, it is the table again with
 * extra steps. `topN` defaults to 5 so the total (5 + "other") never exceeds a 6-color
 * palette with no repeats.
 */
export function failureCauseRows(failures: MigrationFailure[] | undefined, topN = 5) {
  const list = [...(failures ?? [])].sort((a, b) => b.count - a.count)
  const top = list.slice(0, topN)
  const rest = list.slice(topN)
  const rows = top.map((f) => ({
    name: f.reason.length > 48 ? `${f.reason.slice(0, 45)}...` : f.reason,
    value: f.count, reason: f.reason, itemType: f.itemType,
  }))
  const restTotal = rest.reduce((a, f) => a + f.count, 0)
  if (restTotal > 0) {
    rows.push({
      name: `${rest.length} other cause${rest.length === 1 ? '' : 's'}`,
      value: restTotal, itemType: '',
      reason: rest.map((f) => f.reason).slice(0, 5).join('; ')
        + (rest.length > 5 ? `, and ${rest.length - 5} more` : ''),
    })
  }
  return rows
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
