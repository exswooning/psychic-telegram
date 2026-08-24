import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import MigrationDetail from './MigrationDetail'

const fetchMigrationDetail = vi.fn()
const startDelta = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMigrationDetail: (...a: unknown[]) => fetchMigrationDetail(...a),
  startDelta: (...a: unknown[]) => startDelta(...a),
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
    expect(screen.getByTestId('run-delta')).not.toBeDisabled()
  })

  it('refuses to start one while a migration is running', async () => {
    /* Delta uses the same engine and the same machine-wide capacity slot,
       so starting it mid-run would be refused by job_admission anyway --
       better to say so before asking for a Reason Code. */
    show(detail({ running: true }))
    await waitFor(() => expect(screen.getByTestId('run-delta')).toBeTruthy())
    expect(screen.getByTestId('run-delta')).toBeDisabled()
    expect(screen.getByTestId('run-delta')).toHaveTextContent('migration running')
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

  it('never lets the window fall below one day', async () => {
    /* A zero-day window asks the source what changed in no time at all --
       a pass that is guaranteed to copy nothing while consuming a slot. */
    show(detail({ running: false }))
    await waitFor(() => expect(screen.getByTestId('delta-days')).toBeTruthy())
    fireEvent.change(screen.getByTestId('delta-days'), { target: { value: '0' } })
    expect((screen.getByTestId('delta-days') as HTMLInputElement).value).toBe('1')
  })
})
