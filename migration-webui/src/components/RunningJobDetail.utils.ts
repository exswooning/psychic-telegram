/** Straight-line projection from what has elapsed and what is done.
 *  Only honest while the rate holds, which is why it is labelled as a
 *  projection and rendered "--" rather than 0 when there is nothing to
 *  project from. */
export function projectedEta(pct: number | null | undefined,
                             elapsedSec: number | undefined): number | null {
  if (typeof pct !== 'number' || !elapsedSec || pct <= 0 || pct >= 100) return null
  return (elapsedSec / pct) * (100 - pct)
}

/** What a job's own output measures, for every job that has no dashboard of its own.
 *
 *  `done`/`total` come from the newest [done/total] counter in the phase that is running
 *  now -- a finished phase's [300/300] says nothing about the next one. The rate is
 *  measured from the run (done since it started), not assumed. Failures are every line
 *  the job itself marks as one; results are its own summary lines. Nothing is guessed:
 *  a job that prints no counter gets nulls, which the window shows as "--". */
export interface JobFacts {
  done: number | null
  total: number | null
  perMinute: number | null
  etaSec: number | null
  failures: string[]
  warnings: string[]
  results: string[]
}

/** A verification run reports work by user and service. The usual [done/total]
 *  counter advances only after a whole user finishes, which left an exhaustive
 *  run with no ETA for hours. Count each completed service plus the latest
 *  item fraction for services still in flight. This is deliberately an
 *  approximate service-weighted projection; service sizes vary, so the UI says
 *  so and recalculates as the run progresses. */
export interface VerificationEstimate {
  doneServices: number
  totalServices: number
  progressPct: number
  etaSec: number | null
}

export interface VerificationServiceMetrics {
  service: string
  finished: number
  active: number
  activeItems: number
  activeTotal: number
  checked: number
  identical: number
  differences: number
  missing: number
  errors: number
}

export interface VerificationMetrics extends VerificationEstimate {
  usersExpected: number
  completedUsers: number
  activeUsers: number | null
  pendingUsers: number | null
  workers: number | null
  maxWorkers: number | null
  checked: number
  inFlightItems: number
  identical: number
  differences: number
  missing: number
  errors: number
  itemsPerMinute: number | null
  services: VerificationServiceMetrics[]
}

export function verificationMetrics(lines: string[], elapsedSec: number | null | undefined): VerificationMetrics | null {
  let start = -1
  let users = 0
  let servicesPerUser = 0
  let activeUsers: number | null = null
  let pendingUsers: number | null = null
  let workers: number | null = null
  let maxWorkers: number | null = null
  lines.forEach((line, i) => {
    const m = /^VERIFY START: (\d+) user\(s\), (\d+) service\(s\) each, (\d+) worker\(s\)(?:, mode=[^,]+, maxWorkers=(\d+))?/.exec(line)
    if (m) {
      start = i; users = Number(m[1]); servicesPerUser = Number(m[2])
      workers = Number(m[3]); maxWorkers = m[4] ? Number(m[4]) : null
    }
  })
  if (start < 0 || users <= 0 || servicesPerUser <= 0) return null

  const finished = new Set<string>()
  const active = new Map<string, { service: string; done: number; total: number | null }>()
  const perService = new Map<string, VerificationServiceMetrics>()
  const key = (user: string, service: string) => `${user.toLowerCase()}|${service.toLowerCase()}`
  const number = (value: string) => Number(value.replace(/,/g, ''))
  const serviceMetrics = (name: string) => {
    const k = name.toLowerCase()
    let value = perService.get(k)
    if (!value) {
      value = { service: name, finished: 0, active: 0, activeItems: 0, activeTotal: 0,
        checked: 0, identical: 0, differences: 0, missing: 0, errors: 0 }
      perService.set(k, value)
    }
    return value
  }
  let completedUsers = 0

  for (const line of lines.slice(start + 1)) {
    let m = /^verify: scheduler activeUsers=(\d+) pendingUsers=(\d+) completedUsers=(\d+) workers=(\d+) maxWorkers=(\d+)/i.exec(line)
    if (m) {
      activeUsers = Number(m[1]); pendingUsers = Number(m[2])
      workers = Number(m[4]); maxWorkers = Number(m[5])
      completedUsers = Math.max(completedUsers, Number(m[3]))
      continue
    }
    m = /^verify: (.+?) \| service \d+ of \d+ ([a-z_-]+) started\b/i.exec(line)
    if (m) {
      active.set(m[1], { service: m[2], done: 0, total: null })
      serviceMetrics(m[2])
      continue
    }
    m = /^verify: (.+?) \| ([a-z_-]+) finished in [\d.]+s;/i.exec(line)
    if (m) {
      const k = key(m[1], m[2])
      finished.add(k)
      const stats = serviceMetrics(m[2])
      stats.finished += 1
      const counts = /(\d[\d,]*) checked,\s*(\d[\d,]*) identical,\s*(\d[\d,]*) difference\(s\),\s*(\d[\d,]*) missing,\s*(\d[\d,]*) incomplete\/error\(s\)/i.exec(line)
      if (counts) {
        stats.checked += number(counts[1])
        stats.identical += number(counts[2])
        stats.differences += number(counts[3])
        stats.missing += number(counts[4])
        stats.errors += number(counts[5])
      }
      active.delete(m[1])
      continue
    }
    m = /^verify: \[(\d+)\/\d+\] .+ checked;/i.exec(line)
    if (m) completedUsers = Math.max(completedUsers, Number(m[1]))
    m = /^verify: (?:heartbeat|progress) (.+?) \| service=([^|]+).*?\| items=([\d,]+)(?:\/([\d,]+))?/i.exec(line)
    if (m) {
      const state = active.get(m[1])
      if (state && state.service.toLowerCase() === m[2].trim().toLowerCase()) {
        state.done = number(m[3])
        state.total = m[4] ? number(m[4]) : null
      }
    }
  }

  let inFlightItems = 0
  for (const state of active.values()) {
    const stats = serviceMetrics(state.service)
    stats.active += 1
    stats.activeItems += state.done
    stats.activeTotal += state.total ?? 0
    inFlightItems += state.done
  }
  const totalServices = users * servicesPerUser
  const activeFraction = [...active.values()].reduce((sum, state) => {
    if (!state.total || state.total <= 0) return sum
    return sum + Math.max(0, Math.min(1, state.done / state.total))
  }, 0)
  const doneServices = Math.min(totalServices, finished.size + activeFraction)
  const progressPct = Math.min(100, (doneServices / totalServices) * 100)
  const elapsed = elapsedSec ?? 0
  const etaSec = doneServices > 0 && elapsed > 0 && doneServices < totalServices
    ? elapsed * (totalServices - doneServices) / doneServices : null
  const services = [...perService.values()]
  const checked = services.reduce((sum, item) => sum + item.checked, 0) + inFlightItems
  const identical = services.reduce((sum, item) => sum + item.identical, 0)
  const differences = services.reduce((sum, item) => sum + item.differences, 0)
  const missing = services.reduce((sum, item) => sum + item.missing, 0)
  const errors = services.reduce((sum, item) => sum + item.errors, 0)
  const itemsPerMinute = elapsed > 0 && checked > 0 ? checked * 60 / elapsed : null
  return { doneServices, totalServices, progressPct, etaSec, usersExpected: users, completedUsers,
    activeUsers, pendingUsers, workers, maxWorkers,
    checked, inFlightItems, identical, differences, missing, errors,
    itemsPerMinute, services }
}

export function verificationEstimate(lines: string[], elapsedSec: number | null | undefined): VerificationEstimate | null {
  const metrics = verificationMetrics(lines, elapsedSec)
  if (!metrics) return null
  const { doneServices, totalServices, progressPct, etaSec } = metrics
  return { doneServices, totalServices, progressPct, etaSec }
}

const COUNTER = /\[(\d+)\s*\/\s*(\d+)\]/
const PHASE = /^\s*(?:=== run started\b|Seeding \d+ users? in\b|About to DELETE all\b|Mirror pass:|SOURCE ACCESS:|PASS \d+\/\d+\s|VERIFY START:)/
const FAILURE = /^\s*!|\bFAILED\b|Traceback \(most recent|^\s*\w*Error:|NOT (?:WIPED|RESET)|could not /
const QUOTA = /HTTP\s*429|RESOURCE_EXHAUSTED|rateLimitExceeded|userRateLimitExceeded/i
const RESULT = /^\s*(?:Removed:|Removed |deleted [\d,]+, failed|ledger (?:invalidated|left alone)|Ledger reset|Checked [\d,]+|NOT WIPED|NOT RESET|MIRROR |verification:|tally: done)/

export function jobFacts(lines: string[], elapsedSec: number | null | undefined): JobFacts {
  let start = 0
  let runStart = 0
  lines.forEach((l, i) => {
    if (PHASE.test(l)) start = i
    if (/^=== run started\b/.test(l)) runStart = i
  })
  const currentRun = lines.slice(runStart)
  let done: number | null = null
  let total: number | null = null
  for (const l of lines.slice(start)) {
    const m = COUNTER.exec(l)
    if (!m) continue
    const d = Number(m[1]); const t = Number(m[2])
    if (t !== total) { total = t; done = d }       // a new counter in the same phase
    else if (done === null || d > done) done = d   // completion order: the largest is the truth
  }
  const minutes = elapsedSec ? elapsedSec / 60 : 0
  const perMinute = done !== null && done > 0 && minutes > 0 ? done / minutes : null
  const etaSec = perMinute && total !== null && done !== null && done < total
    ? ((total - done) / perMinute) * 60 : null
  return {
    done, total, perMinute, etaSec,
    failures: currentRun.filter((l) => FAILURE.test(l)),
    // Quota responses are often recoverable, so keep them distinct from
    // job failures while still surfacing them outside the raw log.
    warnings: currentRun.filter((l) => QUOTA.test(l)),
    results: currentRun.filter((l) => RESULT.test(l)),
  }
}

/** Verification has a useful counter in its own output. Prefer it to a
 *  stale server percentage (for example one inherited from an earlier run). */
export function jobProgressPct(name: string, fallback: number | null,
                               lines: string[] = []): number | null {
  if (name.trim().toLowerCase() !== 'verify') return fallback
  const { done, total } = jobFacts(lines, undefined)
  return done !== null && total !== null && total > 0
    ? Math.min(100, (done / total) * 100)
    : fallback
}
