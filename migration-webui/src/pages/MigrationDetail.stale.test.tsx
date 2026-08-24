import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import MigrationDetail from './MigrationDetail'

const fetchMigrationDetail = vi.fn()

vi.mock('@/api/controlPlane', () => ({
  fetchMigrationDetail: (...a: unknown[]) => fetchMigrationDetail(...a),
  startDelta: vi.fn(),
  // The page also surveys its failures. A mock missing an export the
  // component calls throws inside render, which surfaces as every assertion
  // failing rather than as the one missing name.
  fetchRepairSurvey: () => Promise.resolve({
    accountId: 7, total: 0, families: [], unclassified: 0, error: '' }),
  runRepair: vi.fn(),
}))
vi.mock('@/components/ReasonCodeDialog', () => ({ default: () => null }))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ accountId: '7' }),
  useNavigate: () => vi.fn(),
}))

/**
 * A failure recorded before the current run started is a queued retry, not a
 * live problem. Rendering the two identically put 160 users on screen as
 * broken -- with 18-hour-old "invalid_grant" text, against target accounts
 * that had since been deleted and recreated -- while the run retrying them
 * was working fine.
 */
const detail = (over: Record<string, unknown> = {}) => ({
  accountId: 7, sourceDomain: 'a.com', targetDomain: 'b.com',
  running: true, error: '',
  progress: { users: 201, done: 1, running: 26, pending: 14, failed: 160,
              blocked: 0, items: 65653, itemsFailed: 250505 },
  items: [], failures: [], users: [],
  runStartedAt: '2026-08-22T04:30:00Z',
  failedUsers: [
    { sourceUser: 'old@src', targetUser: 'old@tgt', status: 'FAILED',
      statusAt: '2026-08-21T17:33:00Z', detail: 'invalid_grant' },
    { sourceUser: 'new@src', targetUser: 'new@tgt', status: 'FAILED',
      statusAt: '2026-08-22T05:00:00Z', detail: 'something current' },
  ],
  ...over,
})

describe('MigrationDetail stale failures', () => {
  beforeEach(() => { fetchMigrationDetail.mockReset() })

  it('marks a failure from before this run as a queued retry', async () => {
    fetchMigrationDetail.mockResolvedValue(detail())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('stale-old@src')).toBeTruthy())
    expect(screen.queryByTestId('stale-new@src')).toBeNull()
  })

  it('summarises how many are stale', async () => {
    fetchMigrationDetail.mockResolvedValue(detail())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('stale-note')).toBeTruthy())
    expect(screen.getByTestId('stale-note').textContent).toContain('1 of these')
  })

  it('says nothing when every failure is current', async () => {
    fetchMigrationDetail.mockResolvedValue(detail({
      failedUsers: [{ sourceUser: 'new@src', targetUser: 'new@tgt',
                      status: 'FAILED', statusAt: '2026-08-22T05:00:00Z',
                      detail: 'current' }],
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('failed-users')).toBeTruthy())
    expect(screen.queryByTestId('stale-note')).toBeNull()
  })

  it('does not guess "old" when the timestamp is unknown', async () => {
    // Ledgers written before status_at existed have no timestamp. Guessing
    // stale would hide a real failure; the two errors are not symmetric.
    fetchMigrationDetail.mockResolvedValue(detail({
      failedUsers: [{ sourceUser: 'legacy@src', targetUser: 'legacy@tgt',
                      status: 'FAILED', detail: 'no timestamp' }],
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('failed-users')).toBeTruthy())
    expect(screen.queryByTestId('stale-legacy@src')).toBeNull()
    expect(screen.queryByTestId('stale-note')).toBeNull()
  })

  it('does not mark anything stale when the run start is unknown', async () => {
    fetchMigrationDetail.mockResolvedValue(detail({ runStartedAt: '' }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('failed-users')).toBeTruthy())
    expect(screen.queryByTestId('stale-old@src')).toBeNull()
  })
})
