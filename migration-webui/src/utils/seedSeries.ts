/**
 * What a seed run measured, shaped for charts. Pure and separate from the
 * components for the same reason as metricsSeries: the shaping is where a
 * chart quietly misleads, and it deserves a test that needs no browser.
 */
import type { FillSample, SeedRun, SeedUser, ThrottleSample } from '@/utils/seedLog'

const local = (email: string) => email.split('@')[0]
const finished = (u: SeedUser) => u.status !== 'running'
/** Filler files are ballast for the storage fill, not seeded content. */
const items = (u: SeedUser) =>
  Object.entries(u.counts ?? {}).reduce((n, [k, v]) => n + (k === 'filler file' ? 0 : v), 0)

/** Cumulative items after each user finishes, in the order they finished.
 *  The x axis is "users finished", not a clock: a user's own duration is not
 *  the moment it finished (workers start at different times), so a time axis
 *  built from it would be a fabrication. */
export function itemsAsUsersFinish(users: SeedUser[]) {
  let sum = 0
  return users.filter(finished).map((u, i) => {
    sum += items(u)
    return { users: i + 1, items: sum }
  })
}

/** Users per duration band -- the shape a single average hides. */
export function durationBands(users: SeedUser[], bands = 8) {
  const secs = users.filter((u) => finished(u) && typeof u.elapsedSec === 'number')
    .map((u) => u.elapsedSec as number)
  if (secs.length < 2) return []
  const lo = Math.min(...secs), hi = Math.max(...secs)
  if (hi === lo) return [{ range: `${Math.round(lo)}s`, users: secs.length }]
  const w = (hi - lo) / bands
  const out = Array.from({ length: bands }, (_, i) => ({
    range: `${Math.round(lo + i * w)}–${Math.round(lo + (i + 1) * w)}s`, users: 0,
  }))
  for (const s of secs) out[Math.min(bands - 1, Math.floor((s - lo) / w))].users += 1
  return out
}

export function slowestUsers(users: SeedUser[], n = 10) {
  return users.filter((u) => finished(u) && typeof u.elapsedSec === 'number')
    .sort((a, b) => (b.elapsedSec as number) - (a.elapsedSec as number))
    .slice(0, n).map((u) => ({ user: local(u.email), seconds: u.elapsedSec as number }))
}

export function warningRows(run: SeedRun, n = 10) {
  return [...run.warnings].sort((a, b) => b.count - a.count).slice(0, n)
    .map((w) => ({ name: w.code ? `${w.kind} · ${w.code}` : w.kind, count: w.count }))
}

/** Services that said they failed inside a user's own "done" line -- a user
 *  can read as finished while producing nothing for two of its services. */
export function failedServiceRows(users: SeedUser[]) {
  const by = new Map<string, number>()
  for (const u of users) for (const s of u.failedServices) by.set(s, (by.get(s) ?? 0) + 1)
  return [...by.entries()].map(([service, count]) => ({ service, count }))
    .sort((a, b) => b.count - a.count)
}

const gb = (n: number) => Math.round(n * 100) / 100

/** Biggest storage additions first; only users that reported one. */
export function storageRows(users: SeedUser[], n = 15) {
  return users.filter((u) => u.storage)
    .map((u) => ({ user: local(u.email), before: gb(u.storage!.beforeGb),
                   added: gb(Math.max(0, u.storage!.afterGb - u.storage!.beforeGb)) }))
    .sort((a, b) => b.added - a.added).slice(0, n)
}

export function storageTotals(users: SeedUser[]) {
  const s = users.filter((u) => u.storage)
  if (!s.length) return null
  return {
    users: s.length,
    addedGb: gb(s.reduce((n, u) => n + Math.max(0, u.storage!.afterGb - u.storage!.beforeGb), 0)),
    fillers: s.reduce((n, u) => n + (u.counts?.['filler file'] ?? 0), 0),
  }
}

/** Uploaded and planned gigabytes at each heartbeat, on the run's own clock. */
export const fillRows = (samples: FillSample[] | undefined) =>
  (samples ?? []).map((f) => ({ t: `${Math.round(f.sec / 60)}m`,
                                uploaded: f.uploadedGb, planned: f.plannedGb }))

/** GB/hour over the last few heartbeats -- measured, and null until there are
 *  two of them rather than a rate from a single point. */
export function fillRateGbPerHour(samples: FillSample[] | undefined, last = 10) {
  const w = (samples ?? []).slice(-last)
  if (w.length < 2) return null
  const dt = w[w.length - 1].sec - w[0].sec
  const dgb = w[w.length - 1].uploadedGb - w[0].uploadedGb
  return dt > 0 && dgb >= 0 ? (dgb / dt) * 3600 : null
}

const minutes = (sec: number) => `${Math.round(sec / 60)}m`

/** GB/hour uploaded between each pair of heartbeats -- the fill's own rate,
 *  which dips when Google or the box slows down. Measured per interval, so it
 *  moves; the tile's rate is an average over the last few. */
export function fillRateRows(samples: FillSample[] | undefined) {
  const s = samples ?? []
  const out: { t: string; gbPerHour: number }[] = []
  for (let i = 1; i < s.length; i++) {
    const dt = s[i].sec - s[i - 1].sec
    const dgb = s[i].uploadedGb - s[i - 1].uploadedGb
    if (dt > 0 && dgb >= 0) out.push({ t: minutes(s[i].sec), gbPerHour: Math.round((dgb / dt) * 3600) })
  }
  return out
}

/** Request rate, and the retries added in each interval. A retry is Google
 *  saying no; the seeder waits and tries again rather than tuning its own
 *  rate, so unlike the migration there is no controller to sawtooth -- bursts
 *  of retries are the closest thing. The retry count on the heartbeat is
 *  cumulative, so the interval's share is the difference. */
export function throttleRows(samples: ThrottleSample[] | undefined) {
  const s = samples ?? []
  return s.map((x, i) => ({
    t: minutes(x.sec), reqPerSec: x.reqPerSec,
    retries: i === 0 ? x.retried : Math.max(0, x.retried - s[i - 1].retried),
  }))
}
