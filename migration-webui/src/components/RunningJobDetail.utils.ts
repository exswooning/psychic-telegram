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
  results: string[]
}

const COUNTER = /\[(\d+)\s*\/\s*(\d+)\]/
const PHASE = /^\s*(?:Seeding \d+ users? in\b|About to DELETE all\b|Mirror pass:)/
const FAILURE = /^\s*!|\bFAILED\b|Traceback \(most recent|^\s*\w*Error:|NOT (?:WIPED|RESET)|could not /
const RESULT = /^\s*(?:Removed:|Removed |deleted [\d,]+, failed|ledger (?:invalidated|left alone)|Ledger reset|Checked [\d,]+|NOT WIPED|NOT RESET|MIRROR |verification:|tally: done)/

export function jobFacts(lines: string[], elapsedSec: number | null | undefined): JobFacts {
  let start = 0
  lines.forEach((l, i) => { if (PHASE.test(l)) start = i })
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
    failures: lines.filter((l) => FAILURE.test(l)),
    results: lines.filter((l) => RESULT.test(l)),
  }
}
