import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import MigrationDetail from './MigrationDetail'

const fetchMigrationDetail = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMigrationDetail: (...a: unknown[]) => fetchMigrationDetail(...a),
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
  progress: { users: 201, done: 199, running: 0, failed: 2, pending: 0,
              items: 242234, itemsFailed: 30 },
  items: [{ type: 'message', count: 240731 }, { type: 'file', count: 82 }],
  failures: [
    { reason: 'HTTP 400 (INVALID_ARGUMENT): Fields with source ids are not allowed.',
      itemType: 'contact', count: 50,
      users: ['tom@source.example.com', 'uma@source.example.com'] },
    { reason: 'HTTP 400 (failedPrecondition): Mail service not enabled',
      itemType: 'user', count: 2, users: ['zane@source.example.com'] },
  ],
  failedUsers: [
    { sourceUser: 'zane@source.example.com', targetUser: 'zane@target.example.com',
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

  it('separates users failed from items failed', async () => {
    /* Two users failed; thirty items did. Collapsing them into one number
       makes a widespread item failure look like a couple of bad mailboxes. */
    show(detail())
    await waitFor(() => expect(screen.getByTestId('stat-failed')).toBeTruthy())
    expect(screen.getByTestId('stat-failed')).toHaveTextContent('2')
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
