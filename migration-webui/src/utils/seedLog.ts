/**
 * seed_sandbox.py has no structured progress protocol -- it is a CLI
 * script whose only interface is what it prints. But what it prints is
 * genuinely rich, and almost all of it was being thrown away by the UI:
 * a run announces its own scale, worker count, per-user expectations, and
 * its own estimate of total API writes and wall-clock minutes, then emits
 * a fully itemised per-user result line (files, folders, comments,
 * messages, drafts, events, calendars, chat, contacts, tasks) and a
 * warning line for every service that partially failed.
 *
 * This parses all of it, so the UI can show what the run actually measured
 * instead of a percentage and a wall of text. Everything here is read from
 * real printed output -- nothing is estimated by this module, and a field
 * that was never printed stays undefined rather than defaulting to 0,
 * because "not reported" and "reported as zero" are different facts.
 */

export interface SeedUser {
  email: string
  status: 'running' | 'done' | 'topped-up'
  /** Department + project, from the "starting" line. */
  context?: string
  /** Seconds this user took, from the "done in Ns:" line. */
  elapsedSec?: number
  /** Itemised counts from the done line, by label. Absent while running. */
  counts?: Record<string, number>
  /** Services that reported a failure inside this user's own done line. */
  failedServices: string[]
}

export interface SeedWarningGroup {
  /** e.g. "label", "chat", "contacts" -- the subject seed_sandbox names. */
  kind: string
  /** e.g. "HTTP 409 (aborted)" -- absent when the line carried no code. */
  code?: string
  count: number
  /** One real example, verbatim, so the group is never just an opaque tally. */
  sample: string
}

export interface SeedRun {
  domain?: string
  scale?: string
  totalUsers?: number
  workers?: number
  workerReason?: string
  estimatedWrites?: number
  estimatedMinutes?: number
  externalCollaborator?: string
  perUserExpectation?: string
  users: SeedUser[]
  /** Sum of every finished user's counts, by label. */
  totals: Record<string, number>
  warnings: SeedWarningGroup[]
  /** Users whose "starting" line was seen but which have not finished. */
  runningCount: number
  doneCount: number
}

// "  [user@domain] starting (Engineering, PRJ-001-Apollo)"
// "  [user@domain] done in 5110.6s: 2271 files, 54 folders, ..."
// "  [user@domain] top-up in 12.3s: 8.1GB -> 9.4GB (3 filler file(s))"
const USER_LINE = /^\s*\[([^\]]+)\]\s+(starting|done|top-up)\b\s*(.*)$/
// "  ! chat for user@x: HTTP 404 (NOT_FOUND): <HttpError ...>"
// "  ! label Clients/Acme: HTTP 409 (aborted): <HttpError ...>"
const WARNING_LINE = /^\s*!\s+(\S+)\b(.*)$/
const HTTP_CODE = /HTTP\s+(\d{3})\s*(?:\(([^)]+)\))?/

// Longest-first: "chat messages" and "secondary calendars" must be tried
// before "messages"/"calendars", or the shorter alternative matches inside
// them and mis-labels the count.
const COUNT_LABELS = [
  'chat messages', 'secondary calendars', 'filler file', 'files', 'folders',
  'comments', 'messages', 'drafts', 'events', 'spaces', 'contacts', 'tasks',
]
const COUNT_RE = new RegExp(String.raw`(\d[\d,]*)\s+(${COUNT_LABELS.join('|')})`, 'g')

const num = (s: string) => Number(s.replace(/,/g, ''))

function parseCounts(detail: string): Record<string, number> {
  const out: Record<string, number> = {}
  for (const m of detail.matchAll(COUNT_RE)) {
    // First occurrence wins: a label repeated later in the same line is
    // part of a parenthesised failure note, not a second real count.
    if (!(m[2] in out)) out[m[2]] = num(m[1])
  }
  return out
}

/**
 * Which services said they failed inside a user's own done line --
 * seed_sandbox appends "(chat failed (...))" / "(contacts failed (...))"
 * rather than emitting a separate error, so a user can read as finished
 * while having produced nothing at all for two of its services.
 */
function parseFailedServices(detail: string): string[] {
  return [...detail.matchAll(/(\w+) failed \(/g)].map((m) => m[1])
}

export function parseSeedRun(lines: string[]): SeedRun {
  const byUser = new Map<string, SeedUser>()
  const warnings = new Map<string, SeedWarningGroup>()
  const run: SeedRun = {
    users: [], totals: {}, warnings: [], runningCount: 0, doneCount: 0,
  }

  for (const line of lines) {
    const user = line.match(USER_LINE)
    if (user) {
      const [, email, verb, rest] = user
      const detail = rest.trim()
      if (verb === 'starting') {
        byUser.set(email, {
          email, status: 'running', context: detail || undefined, failedServices: [],
        })
      } else {
        const elapsed = detail.match(/in\s+([\d.]+)s/)
        byUser.set(email, {
          email,
          status: verb === 'done' ? 'done' : 'topped-up',
          context: byUser.get(email)?.context,
          elapsedSec: elapsed ? Number(elapsed[1]) : undefined,
          counts: parseCounts(detail),
          failedServices: parseFailedServices(detail),
        })
      }
      continue
    }

    const warn = line.match(WARNING_LINE)
    if (warn) {
      const kind = warn[1]
      const code = line.match(HTTP_CODE)
      const label = code ? `HTTP ${code[1]}${code[2] ? ` (${code[2]})` : ''}` : undefined
      const key = `${kind}|${label ?? ''}`
      const existing = warnings.get(key)
      if (existing) existing.count += 1
      else warnings.set(key, { kind, code: label, count: 1, sample: line.trim() })
      continue
    }

    let m
    if ((m = line.match(/Seeding\s+(\d+)\s+users?\s+in\s+(\S+)\s+at\s+scale\s+'([^']+)'/))) {
      run.totalUsers = num(m[1]); run.domain = m[2]; run.scale = m[3]
    } else if ((m = line.match(/^\s*Workers:\s*(\d+)\s*(?:\(([^)]*)\))?/))) {
      run.workers = num(m[1]); run.workerReason = m[2] || undefined
    } else if ((m = line.match(/estimated\s+~?([\d,]+)\s+API writes,\s+roughly\s+([\d,]+)\s+minute/))) {
      run.estimatedWrites = num(m[1]); run.estimatedMinutes = num(m[2])
    } else if ((m = line.match(/external collaborator:\s*(\S+)/))) {
      run.externalCollaborator = m[1]
    } else if ((m = line.match(/^\s*(~\d+\s+messages\s+and\s+~\d+\s+events\s+per\s+user)/))) {
      run.perUserExpectation = m[1]
    } else if (run.totalUsers === undefined
               && (m = line.match(/Found\s+(\d+)\s+existing\s+user/))) {
      // Only as a fallback: the "Seeding N users" line is authoritative
      // (it reflects --users/--all-users filtering; this one does not).
      run.totalUsers = num(m[1])
    }
  }

  run.users = [...byUser.values()]
  for (const u of run.users) {
    if (u.status === 'running') run.runningCount += 1
    else run.doneCount += 1
    for (const [label, n] of Object.entries(u.counts ?? {})) {
      run.totals[label] = (run.totals[label] ?? 0) + n
    }
  }
  run.warnings = [...warnings.values()].sort((a, b) => b.count - a.count)
  return run
}
