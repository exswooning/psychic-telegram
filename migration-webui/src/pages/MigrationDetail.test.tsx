import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import MigrationDetail from './MigrationDetail'

const fetchMigrationDetail = vi.fn()
const startDelta = vi.fn()
const startMigration = vi.fn()
vi.mock('@/components/QuickVerification', () => ({
  default: ({ accountId }: { accountId: number }) => <div data-testid="quick-panel">{accountId}</div>,
}))
vi.mock('@/components/RunReports', () => ({
  default: ({ accountId }: { accountId?: number }) => <div data-testid="reports-panel">{accountId}</div>,
}))
vi.mock('@/api/controlPlane', () => ({
  fetchMigrationDetail: (...a: unknown[]) => fetchMigrationDetail(...a),
  startDelta: (...a: unknown[]) => startDelta(...a),
  startMigration: (...a: unknown[]) => startMigration(...a),
  // The page also surveys its failures. A mock missing an export the
  // component calls throws inside render, which surfaces as every assertion
  // failing rather than as the one missing name.
  fetchRepairSurvey: () => Promise.resolve({
    accountId: 7, total: 0, families: [], unclassified: 0, error: '' }),
  runRepair: vi.fn(),
}))

/**
 * The report a migration row opens onto.
 *
 * Failures are grouped by CAUSE. A run that fails 50 contacts fails them for
 * one reason, and fifty identical HTTP 400s scrolled down a page hides that
 * entirely -- the count and one example are what anybody acts on.
 */
const detail = (over = {}) => ({
  accountId: 7,
  sourceDomain: 'source.example.com',
  targetDomain: 'target.example.com',
  running: false,
  progress: { users: 201, done: 199, running: 0, failed: 0, pending: 0,
              blocked: 2, items: 242234, itemsFailed: 30 },
  items: [{ type: 'message', count: 240731 }, { type: 'file', count: 82 }],
  failures: [
    { reason: 'HTTP 400 (INVALID_ARGUMENT): Fields with source ids are not allowed.',
      itemType: 'contact', count: 50,
      users: ['tom@source.example.com', 'uma@source.example.com'],
      userCount: 2 },
    { reason: 'HTTP 400 (failedPrecondition): Mail service not enabled',
      itemType: 'user', count: 2, users: ['zane@source.example.com'],
      userCount: 1 },
  ],
  failedUsers: [
    { sourceUser: 'zane@source.example.com', targetUser: 'zane@target.example.com',
      status: 'BLOCKED',
      detail: 'This almost always means the account has no Workspace licence' },
  ],
  users: [
    { sourceUser: 'zane@source.example.com', targetUser: 'zane@target.example.com',
      status: 'FAILED', services: '' },
    { sourceUser: 'ada@source.example.com', targetUser: 'ada@target.example.com',
      status: 'DONE', services: 'drive,gmail' },
  ],
  error: '',
  ...over,
})

const show = (d: unknown) => {
  fetchMigrationDetail.mockResolvedValue(d)
  render(
    <MemoryRouter initialEntries={['/migrations/7']}>
      <Routes>
        <Route path="/migrations/:accountId" element={<MigrationDetail />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('MigrationDetail', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('carries the run reports for this account, even before the detail has loaded', async () => {
    // The saved reports are the answer to "how did it go", so they must not
    // wait on (or depend on) the live detail.
    fetchMigrationDetail.mockReturnValue(new Promise(() => {}))     // never loads
    render(
      <MemoryRouter initialEntries={['/migrations/7']}>
        <Routes><Route path="/migrations/:accountId" element={<MigrationDetail />} /></Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByTestId('reports-panel')).toHaveTextContent('7')
  })

  it('reports errors grouped by cause with a count', async () => {
    show(detail())
    await waitFor(() => expect(screen.getByTestId('failure-0')).toBeTruthy())
    const first = screen.getByTestId('failure-0')
    expect(first).toHaveTextContent('50')
    expect(first).toHaveTextContent('Fields with source ids are not allowed')
    expect(first).toHaveTextContent('contact')
  })

  it('names the affected mailboxes for each cause', async () => {
    /* "which users" is the next question every single time. */
    show(detail())
    await waitFor(() => expect(screen.getByTestId('failure-0')).toBeTruthy())
    expect(screen.getByTestId('failure-0'))
      .toHaveTextContent('tom@source.example.com')
  })

  it('lists users that did not migrate, with the diagnosis', async () => {
    show(detail())
    await waitFor(() => expect(screen.getByTestId('failed-users')).toBeTruthy())
    expect(screen.getByTestId('faileduser-zane@source.example.com'))
      .toHaveTextContent('no Workspace licence')
  })

  it('reports what moved, by type', async () => {
    show(detail())
    await waitFor(() => expect(screen.getByTestId('item-message')).toBeTruthy())
    expect(screen.getByTestId('item-message')).toHaveTextContent('240,731')
    expect(screen.getByTestId('item-file')).toHaveTextContent('82')
  })

  it('counts a licence-blocked user apart from a failure', async () => {
    /* They need opposite responses: one is waited on, the other
       investigated. A count that merges them stops meaning "investigate". */
    show(detail())
    await waitFor(() => expect(screen.getByTestId('stat-blocked')).toBeTruthy())
    expect(screen.getByTestId('stat-blocked')).toHaveTextContent('2')
    expect(screen.getByTestId('stat-failed')).toHaveTextContent('0')
  })

  it('labels a blocked user as waiting rather than broken', async () => {
    show(detail())
    await waitFor(() =>
      expect(screen.getByTestId('faileduser-zane@source.example.com')).toBeTruthy())
    expect(screen.getByTestId('faileduser-zane@source.example.com'))
      .toHaveTextContent('waiting on you')
  })

  it('separates users failed from items failed', async () => {
    /* Two users failed; thirty items did. Collapsing them into one number
       makes a widespread item failure look like a couple of bad mailboxes. */
    show(detail())
    await waitFor(() => expect(screen.getByTestId('stat-failed')).toBeTruthy())
    expect(screen.getByTestId('stat-failed')).toHaveTextContent('0')
    expect(screen.getByTestId('stat-itemsfailed')).toHaveTextContent('30')
  })

  it('says plainly when there are no failures', async () => {
    show(detail({ failures: [], failedUsers: [], users: [],
                  progress: { users: 5, done: 5, running: 0, failed: 0,
                              pending: 0, items: 100, itemsFailed: 0 } }))
    await waitFor(() => expect(screen.getByTestId('no-failures')).toBeTruthy())
    expect(screen.queryByTestId('failed-users')).toBeNull()
  })

  it('surfaces a ledger that cannot be read instead of an empty report', async () => {
    show(detail({ error: 'this account has no migration ledger yet',
                  items: [], failures: [], failedUsers: [], users: [] }))
    await waitFor(() =>
      expect(screen.getByText(/no migration ledger yet/)).toBeTruthy())
  })

  it('shows every user with its state, inside the report', async () => {
    /* Per-user state only means anything against the tenant pair it belongs
       to, so it belongs here rather than on a page that has to guess which
       migration you meant. */
    show(detail())
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    expect(screen.getByTestId('user-ada@source.example.com'))
      .toHaveTextContent('done')
    expect(screen.getByTestId('user-ada@source.example.com'))
      .toHaveTextContent('drive,gmail')
  })

  it('puts failures at the top of the user table', async () => {
    /* A 200-row table sorted alphabetically buries the two rows anybody
       opened this page to find. The server orders it; this pins that the
       page does not re-sort it away. */
    show(detail())
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    const rows = screen.getAllByTestId(/^user-/)
    expect(rows[0]).toHaveTextContent('zane@source.example.com')
  })

  it('offers a way back to the list', async () => {
    show(detail())
    await waitFor(() => expect(screen.getByTestId('back')).toBeTruthy())
  })
})


describe('MigrationDetail — delta pass', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('offers a delta run when the migration is idle', async () => {
    show(detail({ running: false }))
    await waitFor(() => expect(screen.getByTestId('run-delta')).toBeTruthy())
    expect(screen.getByTestId('run-delta')).toHaveTextContent('Run delta')
    expect(screen.getByTestId('run-delta')).not.toBeDisabled()
  })

  it('refuses to start one while a migration is running', async () => {
    /* Delta uses the same engine and the same machine-wide capacity slot,
       so starting it mid-run would be refused by job_admission anyway --
       better to say so before asking for a Reason Code. */
    show(detail({ running: true }))
    // Wait for the LOADED state, not just for the button to exist: it
    // renders before the detail fetch resolves, and in that frame `d` is
    // null so `disabled={d?.running || …}` is false. Waiting on existence
    // alone made this a race that only lost under full-suite load.
    await waitFor(() =>
      expect(screen.getByTestId('run-delta')).toHaveTextContent('migration running'))
    expect(screen.getByTestId('run-delta')).toBeDisabled()
  })

  it('asks for a Reason Code before starting', async () => {
    /* Every write action carries one; a catch-up pass writes into a live
       target like any other. */
    show(detail({ running: false }))
    await waitFor(() => expect(screen.getByTestId('run-delta')).toBeTruthy())
    fireEvent.click(screen.getByTestId('run-delta'))
    await waitFor(() => expect(screen.getByText(/Run a delta pass/)).toBeTruthy())
    expect(startDelta).not.toHaveBeenCalled()
  })

  it('carries the chosen look-back window', async () => {
    show(detail({ running: false }))
    await waitFor(() => expect(screen.getByTestId('delta-days')).toBeTruthy())
    fireEvent.change(screen.getByTestId('delta-days'), { target: { value: '7' } })
    expect((screen.getByTestId('delta-days') as HTMLInputElement).value).toBe('7')
  })

  it('scopes the pass to named users when asked', async () => {
    /* StartDelta has always carried `users`; the client sent [] every time,
       so the only delta reachable from the app was all 200 users. */
    startDelta.mockResolvedValue({ ok: true, detail: 'started' })
    show(detail({ running: false }))
    await waitFor(() => expect(screen.getByTestId('delta-users')).toBeTruthy())
    fireEvent.change(screen.getByTestId('delta-users'),
                     { target: { value: ' a@x.test , b@x.test ' } })
    fireEvent.click(screen.getByTestId('run-delta'))
    await waitFor(() => expect(screen.getByText(/Run a delta pass/)).toBeTruthy())
    const box = document.querySelector('[role="dialog"] input, [role="dialog"] textarea')
    fireEvent.change(box!, { target: { value: 'why-not' } })
    fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(startDelta).toHaveBeenCalled())
    const args = startDelta.mock.calls[0]
    expect(args[4]).toEqual(['a@x.test', 'b@x.test'])
  })

  it('treats a blank scope as the whole batch', async () => {
    startDelta.mockResolvedValue({ ok: true, detail: 'started' })
    show(detail({ running: false }))
    await waitFor(() => expect(screen.getByTestId('run-delta')).toBeTruthy())
    fireEvent.click(screen.getByTestId('run-delta'))
    await waitFor(() => expect(screen.getByText(/Run a delta pass/)).toBeTruthy())
    const box = document.querySelector('[role="dialog"] input, [role="dialog"] textarea')
    fireEvent.change(box!, { target: { value: 'why-not' } })
    fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(startDelta).toHaveBeenCalled())
    expect(startDelta.mock.calls[0][4]).toEqual([])
  })

  it('never lets the window fall below one day', async () => {
    /* A zero-day window asks the source what changed in no time at all --
       a pass that is guaranteed to copy nothing while consuming a slot. */
    show(detail({ running: false }))
    await waitFor(() => expect(screen.getByTestId('delta-days')).toBeTruthy())
    fireEvent.change(screen.getByTestId('delta-days'), { target: { value: '0' } })
    expect((screen.getByTestId('delta-days') as HTMLInputElement).value).toBe('1')
  })
})


/*
 * Who moves the mail. Split is the default: the tool moves the mail that needs its
 * Drive links rewritten and Google's DMS moves the rest -- which is most of a
 * mailbox, so a run that stops at the handoff has not moved most of the mail. The
 * page must say so instead of counting it as skipped or showing 300 of 300 done.
 */
describe('MigrationDetail: who moves the mail', () => {
  beforeEach(() => { vi.clearAllMocks(); startMigration.mockResolvedValue({ ok: true, actionId: 1, detail: 'started' }) })

  const openDialog = async () => {
    show(detail())
    fireEvent.click(await screen.findByTestId('run-full'))
    await screen.findByText('Who moves the mail?')
  }
  const confirm = async () => {
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'full migration' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(startMigration).toHaveBeenCalled())
  }

  it('offers all three and has split selected', async () => {
    await openDialog()
    expect(screen.getByTestId('mail-by-split').querySelector('input')).toBeChecked()
    expect(screen.getByTestId('mail-by-engine').querySelector('input')).not.toBeChecked()
    expect(screen.getByTestId('mail-by-dms').querySelector('input')).not.toBeChecked()
  })

  it('says the DMS has to run AFTER, and that the mail is not on the target until it has', async () => {
    await openDialog()
    const split = screen.getByTestId('mail-by-split')
    expect(split).toHaveTextContent('after this run finishes')
    expect(split).toHaveTextContent('most of the mail is not on the target')
    expect(split).toHaveTextContent('Drive first, for every user')
  })

  it('starts a split run by default, and asks the server for it', async () => {
    await openDialog(); await confirm()
    const [, services, users, dry, account, mode] = startMigration.mock.calls[0]
    expect([services, users, dry, account, mode]).toEqual([['all'], [], false, 7, 'split'])
  })

  it('sends the mode chosen, and never decides the service list itself', async () => {
    await openDialog()
    fireEvent.click(screen.getByTestId('mail-by-dms').querySelector('input')!)
    await confirm()
    expect(startMigration.mock.calls[0][1]).toEqual(['all'])          // the server drops mail for dms
    expect(startMigration.mock.calls[0][5]).toBe('dms')
  })

  it('asks the server to start the DMS itself, unless the box is unticked', async () => {
    await openDialog(); await confirm()
    expect(startMigration.mock.calls[0][7]).toBeUndefined()
  })

  it('sends dms_after=false when the box is unticked', async () => {
    await openDialog()
    fireEvent.click(screen.getByLabelText('start the DMS automatically'))
    await confirm()
    expect(startMigration.mock.calls[0][7]).toBe(false)
  })

  it('offers no such box when this tool moves the mail', async () => {
    await openDialog()
    fireEvent.click(screen.getByTestId('mail-by-engine').querySelector('input')!)
    expect(screen.queryByLabelText('start the DMS automatically')).not.toBeInTheDocument()
  })

  it('can still run everything through the tool', async () => {
    await openDialog()
    fireEvent.click(screen.getByTestId('mail-by-engine').querySelector('input')!)
    await confirm()
    expect(startMigration.mock.calls[0][5]).toBe('engine')
  })
})

describe('MigrationDetail: what a split run says about itself', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('says plainly that mail is waiting for the DMS, and where to start it', async () => {
    show(detail({ progress: { users: 201, done: 201, running: 0, failed: 0, pending: 0, items: 12000,
                              itemsFailed: 0, itemsSkipped: 3, itemsDeferred: 349560 } }))
    const alert = await screen.findByTestId('awaiting-dms')
    expect(alert).toHaveTextContent('349,560 mail message(s) are waiting')
    expect(alert).toHaveTextContent('not on the target yet')
    expect(alert).toHaveTextContent('after this run has finished')
    expect(screen.getByTestId('to-services')).toBeInTheDocument()
  })

  it('shows deferred mail as its own figure, never folded into skipped', async () => {
    show(detail({ progress: { users: 201, done: 201, running: 0, failed: 0, pending: 0, items: 12000,
                              itemsFailed: 0, itemsSkipped: 3, itemsDeferred: 349560 } }))
    expect(await screen.findByTestId('stat-itemsdeferred')).toHaveTextContent('349,560')
    expect(screen.getByTestId('stat-itemsskipped')).toHaveTextContent('3')
    expect(screen.getByTestId('stat-itemsskipped')).not.toHaveTextContent('349,560')
  })

  it('says nothing about the DMS when nothing is waiting for it', async () => {
    show(detail())
    await screen.findByTestId('stat-users')
    expect(screen.queryByTestId('awaiting-dms')).toBeNull()
    expect(screen.queryByTestId('stat-itemsdeferred')).toBeNull()
  })

  it('says which pass an ordered run is on, so 300 of 300 done is not misread', async () => {
    show(detail({ running: true, run: { pass: 2, of: 3, services: ['gmail'] } }))
    const chip = await screen.findByTestId('run-pass')
    expect(chip).toHaveTextContent('Pass 2 of 3')
    expect(chip).toHaveTextContent('gmail')
    expect(chip).toHaveTextContent('finished the earlier passes')
  })

  it('shows no pass when nothing ordered is running', async () => {
    show(detail({ run: null }))
    await screen.findByTestId('stat-users')
    expect(screen.queryByTestId('run-pass')).toBeNull()
  })
})


/*
 * Quick migrate: a small slice of each user's data, small enough to check one to one.
 * What must hold: it asks the server for a SAMPLE, always through the engine (a
 * sample handed to the DMS could not be compared), names its users on the SOURCE
 * tenant, and refuses a nonsense limit before anything is sent.
 */
describe('MigrationDetail: quick migrate', () => {
  beforeEach(() => { vi.clearAllMocks(); startMigration.mockResolvedValue({ ok: true, actionId: 1, detail: 'started' }) })

  const open = async () => {
    show(detail())
    fireEvent.click(await screen.findByTestId('run-quick'))
    await screen.findByText(/Quick migrate from/)
  }
  const send = async () => {
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'sample check' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
  }
  const field = (id: string) => screen.getByTestId(id) as HTMLInputElement

  it('offers a few users to start with, so one click is a small run', async () => {
    await open()
    expect(field('quick-users').value).toBe('zane@source.example.com, ada@source.example.com')
    expect(field('quick-limit').value).toBe('20')
  })

  it('asks the server for a sample through the engine, for the users and services chosen', async () => {
    await open(); await send()
    await waitFor(() => expect(startMigration).toHaveBeenCalled())
    const [, services, users, dry, account, mode, sample] = startMigration.mock.calls[0]
    expect(services).toEqual(['drive', 'gmail', 'calendar', 'contacts', 'tasks'])       // chat is never sampled
    expect(users).toEqual(['zane@source.example.com', 'ada@source.example.com'])
    expect([dry, account, mode, sample]).toEqual([false, 7, 'engine', 20])
  })

  it('takes a bare name as the address on the SOURCE tenant', async () => {
    await open()
    fireEvent.change(field('quick-users'), { target: { value: 'george, ivan@elsewhere.com' } })
    await send()
    await waitFor(() => expect(startMigration).toHaveBeenCalled())
    expect(startMigration.mock.calls[0][2]).toEqual(['george@source.example.com', 'ivan@elsewhere.com'])
  })

  it('sends the limit and services as changed', async () => {
    await open()
    fireEvent.change(field('quick-limit'), { target: { value: '5' } })
    fireEvent.click(screen.getByTestId('quick-svc-gmail'))
    await send()
    await waitFor(() => expect(startMigration).toHaveBeenCalled())
    expect(startMigration.mock.calls[0][6]).toBe(5)
    expect(startMigration.mock.calls[0][1]).not.toContain('gmail')
  })

  it('says blank means every user, and creates accounts', async () => {
    await open()
    expect(screen.getByText(/Blank means EVERY user/)).toBeInTheDocument()
    expect(screen.getByText(/licences/)).toBeInTheDocument()
  })

  it('says the users are left unfinished, so a full migration still copies the rest', async () => {
    await open()
    expect(screen.getByText(/unfinished/)).toBeInTheDocument()
  })

  it.each(['0', '-3', '1001', '2.5', ''])('refuses %j before anything is sent', async (bad) => {
    await open()
    fireEvent.change(field('quick-limit'), { target: { value: bad } })
    await send()
    expect(await screen.findByText(/whole number from 1 to 1000/)).toBeInTheDocument()
    expect(startMigration).not.toHaveBeenCalled()
  })

  it('refuses no services', async () => {
    await open()
    for (const svc of ['drive', 'gmail', 'calendar', 'contacts', 'tasks']) fireEvent.click(screen.getByTestId(`quick-svc-${svc}`))
    await send()
    expect(await screen.findByText(/Tick at least one service/)).toBeInTheDocument()
    expect(startMigration).not.toHaveBeenCalled()
  })

  it('shows why the server refused it', async () => {
    startMigration.mockResolvedValue({ ok: false, actionId: 1, detail: 'the box is at capacity' })
    await open(); await send()
    expect(await screen.findByText('the box is at capacity')).toBeInTheDocument()
  })

  it('cannot be started while a migration is running', async () => {
    show(detail({ running: true }))
    expect(await screen.findByTestId('run-quick')).toBeDisabled()
  })

  it('puts the saved verification on the page', async () => {
    show(detail())
    expect(await screen.findByTestId('quick-panel')).toHaveTextContent('7')
  })
})

