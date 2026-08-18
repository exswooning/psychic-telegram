/**
 * seed_sandbox.py has no structured progress protocol -- it is a CLI
 * script whose only output is print() lines, printed once per user per
 * lifecycle event (see its own seed_one_user()/top_up_one_user()):
 *
 *   "  [user@domain] starting (Dept, Project)"
 *   "  [user@domain] done in 5110.6s: 2271 files, 54 folders, ..."
 *   "  [user@domain] top-up in 12.3s: 8.1GB -> 9.4GB (3 filler file(s))"
 *
 * Everything else printed (warnings like "! label X: HTTP 409 ...", the
 * banner lines) does not start with "[user]" and is deliberately not
 * matched here -- this is "which users has it touched, and what happened
 * to each", not a re-implementation of the raw transcript.
 */
export interface SeedUserEvent {
  email: string
  status: 'starting' | 'done' | 'top-up'
  detail: string
}

const SEED_USER_LINE = /^\s*\[([^\]]+)\]\s+(starting|done|top-up)\b\s*(.*)$/

/**
 * One entry per user, holding whichever event was printed LAST for them --
 * a user who has a "done" line is finished even though "starting" was
 * printed earlier too, and a user with only "starting" so far is exactly
 * the ones currently in flight (seed_sandbox.py runs several workers in
 * parallel, so this is usually more than one at a time, not just one).
 */
export function parseSeedUsers(lines: string[]): SeedUserEvent[] {
  const byUser = new Map<string, SeedUserEvent>()
  for (const line of lines) {
    const m = line.match(SEED_USER_LINE)
    if (!m) continue
    const [, email, status, rest] = m
    byUser.set(email, { email, status: status as SeedUserEvent['status'], detail: rest.trim() })
  }
  return Array.from(byUser.values())
}
