import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import MigrationDetail from './MigrationDetail'

const fetchMigrationDetail = vi.fn()
const fetchRepairSurvey = vi.fn()
const runRepair = vi.fn()

vi.mock('@/api/controlPlane', () => ({
  fetchMigrationDetail: (...a: unknown[]) => fetchMigrationDetail(...a),
  fetchRepairSurvey: (...a: unknown[]) => fetchRepairSurvey(...a),
  runRepair: (...a: unknown[]) => runRepair(...a),
  startDelta: vi.fn(),
}))
vi.mock('@/components/ReasonCodeDialog', () => ({ default: () => null }))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ accountId: '7' }),
  useNavigate: () => vi.fn(),
}))

/**
 * A failure total is not a diagnosis. Live, 119,600 failures were one live
 * bug plus two families describing states that had since stopped being true
 * -- about six times the number that actually needed anybody.
 */
const detail = (over: Record<string, unknown> = {}) => ({
  accountId: 7, sourceDomain: 'a.com', targetDomain: 'b.com',
  running: false, error: '',
  progress: { users: 201, done: 200, running: 0, pending: 0, failed: 1,
              blocked: 0, items: 784867, itemsFailed: 119600 },
  items: [], failures: [], users: [], failedUsers: [], ...over,
})

const survey = (over: Record<string, unknown> = {}) => ({
  accountId: 7, total: 119600, unclassified: 394, error: '',
  families: [
    { key: 'acl_no_account', count: 58642,
      label: 'share grants refused — the person had no account at the time',
      fix: 'resolvable now' },
    { key: 'gmail_invalid_label', count: 32967,
      label: 'messages rejected — label pointed at a deleted mailbox',
      fix: 'retried on the next migration' },
  ],
  ...over,
})

describe('MigrationDetail repair panel', () => {
  beforeEach(() => {
    fetchMigrationDetail.mockReset(); fetchRepairSurvey.mockReset()
    runRepair.mockReset()
    fetchMigrationDetail.mockResolvedValue(detail())
  })

  it('breaks the failure total into named causes', async () => {
    fetchRepairSurvey.mockResolvedValue(survey())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('repair-panel')).toBeTruthy())
    expect(screen.getByTestId('repair-acl_no_account').textContent).toContain('58,642')
    expect(screen.getByTestId('repair-gmail_invalid_label').textContent)
      .toContain('retried on the next migration')
  })

  it('shows what is not classified rather than hiding it', async () => {
    // A survey that only counts what it recognises reports a friendlier
    // number than the truth.
    fetchRepairSurvey.mockResolvedValue(survey())
    render(<MigrationDetail />)
    await waitFor(() =>
      expect(screen.getByTestId('repair-unclassified')).toBeTruthy())
    expect(screen.getByTestId('repair-unclassified').textContent).toContain('394')
  })

  it('offers the repair button when nothing is running', async () => {
    fetchRepairSurvey.mockResolvedValue(survey())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('run-repair')).toBeTruthy())
    expect(screen.getByTestId('run-repair').hasAttribute('disabled')).toBe(false)
  })

  it('disables it while a migration is running, and says why', async () => {
    // Repair runs automatically at the end of a run; racing it against the
    // migration that is still writing those rows is not useful.
    fetchMigrationDetail.mockResolvedValue(detail({ running: true }))
    fetchRepairSurvey.mockResolvedValue(survey())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('run-repair')).toBeTruthy())
    const btn = screen.getByTestId('run-repair')
    expect(btn.hasAttribute('disabled')).toBe(true)
    expect(btn.textContent).toContain('when the migration finishes')
  })

  it('shows no panel when there is nothing to repair', async () => {
    fetchRepairSurvey.mockResolvedValue(
      survey({ total: 0, families: [], unclassified: 0 }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    expect(screen.queryByTestId('repair-panel')).toBeNull()
  })

  it('survives the survey being unavailable', async () => {
    fetchRepairSurvey.mockRejectedValue(new Error('nope'))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    expect(screen.queryByTestId('repair-panel')).toBeNull()
  })
})

describe('MigrationDetail reports what was skipped', () => {
  beforeEach(() => {
    fetchMigrationDetail.mockReset(); fetchRepairSurvey.mockReset()
    fetchRepairSurvey.mockResolvedValue(
      { accountId: 7, total: 0, families: [], unclassified: 0, error: '' })
  })

  it('shows skipped items alongside migrated and failed', async () => {
    // 56,975 items on a live tenant were neither migrated nor failed, and the
    // page showed nothing between those two numbers.
    fetchMigrationDetail.mockResolvedValue(detail({
      progress: { users: 201, done: 201, running: 0, pending: 0, failed: 0,
                  blocked: 0, items: 818000, itemsFailed: 422,
                  itemsSkipped: 56975 },
    }))
    render(<MigrationDetail />)
    await waitFor(() =>
      expect(screen.getByTestId('stat-itemsskipped')).toBeTruthy())
    expect(screen.getByTestId('stat-itemsskipped').textContent).toContain('56,975')
  })

  it('breaks the skips down by reason', async () => {
    fetchMigrationDetail.mockResolvedValue(detail({
      skipped: [
        { status: 'SKIPPED_GRANTEE_RECREATED', count: 55316 },
        { status: 'SKIPPED_IS_DRAFT', count: 1284 },
      ],
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('skipped-panel')).toBeTruthy())
    expect(screen.getByTestId('skip-SKIPPED_IS_DRAFT').textContent)
      .toContain('1,284')
  })

  it('does not colour a skip as a failure', async () => {
    // Folding decisions into the failure count is how a clean run teaches
    // people to ignore red.
    fetchMigrationDetail.mockResolvedValue(detail({
      skipped: [{ status: 'SKIPPED_IS_DRAFT', count: 12 }],
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('skipped-panel')).toBeTruthy())
    expect(screen.getByTestId('skipped-panel')
      .querySelector('.MuiChip-colorError')).toBeNull()
  })

  it('shows no skip panel when nothing was skipped', async () => {
    fetchMigrationDetail.mockResolvedValue(detail({ skipped: [] }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    expect(screen.queryByTestId('skipped-panel')).toBeNull()
  })
})

describe('MigrationDetail flags broken folder shares', () => {
  beforeEach(() => {
    fetchMigrationDetail.mockReset(); fetchRepairSurvey.mockReset()
    fetchMigrationDetail.mockResolvedValue(detail())
  })

  it('calls out folder shares separately from the total', async () => {
    // Sharing is folder-derived, so a failed folder grant gates every file
    // inside it. 147 folders once accounted for 1,050 inaccessible files
    // while sitting in the same total as 142 single-file failures.
    fetchRepairSurvey.mockResolvedValue(survey({
      brokenFolders: { folders: 147, grants: 265, files_behind: 1050 },
    }))
    render(<MigrationDetail />)
    await waitFor(() =>
      expect(screen.getByTestId('broken-folders')).toBeTruthy())
    const t = screen.getByTestId('broken-folders').textContent || ''
    expect(t).toContain('147')
    expect(t).toContain('1,050')
    expect(t).toContain('folders first')
  })

  it('says nothing when no folder share failed', async () => {
    fetchRepairSurvey.mockResolvedValue(survey({
      brokenFolders: { folders: 0, grants: 0, files_behind: 0 },
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('repair-panel')).toBeTruthy())
    expect(screen.queryByTestId('broken-folders')).toBeNull()
  })

  it('survives the field being absent entirely', async () => {
    fetchRepairSurvey.mockResolvedValue(survey())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('repair-panel')).toBeTruthy())
    expect(screen.queryByTestId('broken-folders')).toBeNull()
  })
})
