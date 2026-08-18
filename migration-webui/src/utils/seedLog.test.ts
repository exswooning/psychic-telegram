import { describe, it, expect } from 'vitest'
import { parseSeedRun } from './seedLog'

/**
 * seed_sandbox.py is a CLI script, not an API -- its printed output IS the
 * interface, so these fixtures are verbatim in shape from a real run
 * against source.rohitrokaya.com.np (201 users, scale 'huge'). Anything
 * that drifts from that format silently empties the dashboard, which is
 * exactly the "UI doesn't reflect the actual work" failure this parser
 * exists to fix -- so the fixtures matter as much as the assertions.
 */
const BANNER = [
  'Sandbox guard passed for source.rohitrokaya.com.np.',
  'Found 201 existing user(s) in source.rohitrokaya.com.np; seeding all of them.',
  'Workers: 9 (memory-bound: 3.0 GB usable / 320 MB per worker = 9)',
  "Seeding 201 users in source.rohitrokaya.com.np at scale 'huge'",
  '  external collaborator: external.tester@example.com',
  '  ~1440 messages and ~480 events per user',
  '  estimated ~2,661,240 API writes, roughly 704 minute(s) at 9 parallel users',
]

const DONE_LINE =
  '  [arjun.gurung82@source.rohitrokaya.com.np] done in 5110.6s: 2271 files, '
  + '54 folders, 133 comments, 1443 messages, 4 drafts, 482 events, '
  + '2 secondary calendars, 0 chat messages in 0 spaces (chat failed (Chat '
  + 'switched on? scopes granted?): HTTP 404 (NOT_FOUND): <HttpError 404 ...>), '
  + '0 contacts (contacts failed (People API enabled? scopes granted?): '
  + 'HTTP 409 (ALREADY_EXISTS): <HttpError 409 ...>), 20 tasks'

describe('run banner', () => {
  it('reads the run identity and sizing the script announces', () => {
    const r = parseSeedRun(BANNER)
    expect(r.domain).toBe('source.rohitrokaya.com.np')
    expect(r.scale).toBe('huge')
    expect(r.totalUsers).toBe(201)
    expect(r.workers).toBe(9)
    expect(r.workerReason).toContain('memory-bound')
    expect(r.externalCollaborator).toBe('external.tester@example.com')
  })

  it('reads the run’s own up-front estimate, commas and all', () => {
    const r = parseSeedRun(BANNER)
    expect(r.estimatedWrites).toBe(2661240)
    expect(r.estimatedMinutes).toBe(704)
  })

  it('prefers "Seeding N users" over "Found N existing" for the total', () => {
    // "Found N" ignores --users/--all-users filtering; "Seeding N" does not.
    const r = parseSeedRun([
      'Found 201 existing user(s) in source.example.com; seeding all of them.',
      "Seeding 3 users in source.example.com at scale 'tiny'",
    ])
    expect(r.totalUsers).toBe(3)
  })

  it('falls back to "Found N existing" when the run has not announced yet', () => {
    const r = parseSeedRun(['Found 201 existing user(s) in source.example.com; seeding all.'])
    expect(r.totalUsers).toBe(201)
  })
})

describe('per-user results', () => {
  it('itemises every count on a done line', () => {
    const r = parseSeedRun([DONE_LINE])
    const u = r.users[0]
    expect(u.email).toBe('arjun.gurung82@source.rohitrokaya.com.np')
    expect(u.status).toBe('done')
    expect(u.elapsedSec).toBe(5110.6)
    expect(u.counts).toEqual({
      files: 2271, folders: 54, comments: 133, messages: 1443, drafts: 4,
      events: 482, 'secondary calendars': 2, 'chat messages': 0, spaces: 0,
      contacts: 0, tasks: 20,
    })
  })

  it('does not let "chat messages" be counted as "messages"', () => {
    // The single most likely way this parser goes quietly wrong: a shorter
    // label matching inside a longer one and overwriting a real figure.
    const r = parseSeedRun([DONE_LINE])
    expect(r.users[0].counts!.messages).toBe(1443)
    expect(r.users[0].counts!['chat messages']).toBe(0)
    expect(r.users[0].counts!['secondary calendars']).toBe(2)
  })

  it('names the services that failed inside an otherwise finished user', () => {
    // A user reads as "done" while having produced nothing for two of its
    // services -- the dashboard has to be able to say so.
    expect(parseSeedRun([DONE_LINE]).users[0].failedServices).toEqual(['chat', 'contacts'])
  })

  it('keeps a user in flight until its own done line arrives', () => {
    const starting = '  [a@x.com] starting (Engineering, PRJ-001-Apollo)'
    const mid = parseSeedRun([...BANNER, starting])
    expect(mid.runningCount).toBe(1)
    expect(mid.doneCount).toBe(0)
    expect(mid.users[0].context).toBe('(Engineering, PRJ-001-Apollo)')

    const after = parseSeedRun([...BANNER, starting,
      '  [a@x.com] done in 10.0s: 5 files, 1 folders'])
    expect(after.runningCount).toBe(0)
    expect(after.doneCount).toBe(1)
    expect(after.users).toHaveLength(1)
  })

  it('handles the top-up pass without treating it as a fresh user', () => {
    const r = parseSeedRun([
      '  [a@x.com] top-up in 12.3s: 8.1GB -> 9.4GB (3 filler file(s))',
    ])
    expect(r.users[0].status).toBe('topped-up')
    expect(r.doneCount).toBe(1)
  })
})

describe('aggregate totals', () => {
  it('sums finished users and leaves in-flight ones out', () => {
    const r = parseSeedRun([
      '  [a@x.com] done in 10s: 100 files, 5 folders',
      '  [b@x.com] done in 20s: 200 files, 7 folders',
      '  [c@x.com] starting (Sales, PRJ-003)',
    ])
    expect(r.totals).toEqual({ files: 300, folders: 12 })
    expect(r.doneCount).toBe(2)
    expect(r.runningCount).toBe(1)
  })

  it('never invents a label the run did not print', () => {
    // "not reported" and "reported as zero" are different facts; the
    // dashboard renders the former as "--" and must be able to tell them
    // apart, which only works if absent labels stay absent here.
    const r = parseSeedRun(['  [a@x.com] done in 10s: 100 files'])
    expect(r.totals).toEqual({ files: 100 })
    expect('contacts' in r.totals).toBe(false)
  })
})

describe('warnings', () => {
  const WARNINGS = [
    '  ! label Clients: HTTP 409 (aborted): <HttpError 409 ...>',
    '  ! label Clients/Acme: HTTP 409 (aborted): <HttpError 409 ...>',
    '  ! label Archived: HTTP 400 (invalidArgument): <HttpError 400 ...>',
    '  ! chat for a@x.com: HTTP 404 (NOT_FOUND): <HttpError 404 ...>',
  ]

  it('groups by subject and HTTP code instead of repeating every line', () => {
    // A real run emits these hundreds of times; the point of grouping is
    // that three distinct problems stay legible among ~1500 lines.
    const r = parseSeedRun(WARNINGS)
    expect(r.warnings).toHaveLength(3)
    const label409 = r.warnings.find((w) => w.kind === 'label' && w.code === 'HTTP 409 (aborted)')
    expect(label409!.count).toBe(2)
  })

  it('orders the groups by how often they actually happened', () => {
    expect(parseSeedRun(WARNINGS).warnings[0].count).toBe(2)
  })

  it('keeps one verbatim example per group', () => {
    const r = parseSeedRun(WARNINGS)
    expect(r.warnings.find((w) => w.kind === 'chat')!.sample)
      .toContain('HttpError 404')
  })

  it('still records a warning that carries no HTTP code', () => {
    const r = parseSeedRun(['  ! top-up for a@x.com: ran out of quota'])
    expect(r.warnings[0].kind).toBe('top-up')
    expect(r.warnings[0].code).toBeUndefined()
  })
})

describe('robustness', () => {
  it('returns an empty run rather than throwing on unrelated output', () => {
    const r = parseSeedRun(['Traceback (most recent call last):', '  File "x.py"', ''])
    expect(r.users).toEqual([])
    expect(r.warnings).toEqual([])
    expect(r.totalUsers).toBeUndefined()
  })

  it('handles an empty transcript', () => {
    const r = parseSeedRun([])
    expect(r.doneCount).toBe(0)
    expect(r.totals).toEqual({})
  })
})
