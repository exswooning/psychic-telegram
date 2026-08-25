import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import MigrationDetail from './MigrationDetail'

const fetchMigrationDetail = vi.fn()
const runRepair = vi.fn()

vi.mock('@/api/controlPlane', () => ({
  fetchMigrationDetail: (...a: unknown[]) => fetchMigrationDetail(...a),
  runRepair: (...a: unknown[]) => runRepair(...a),
  startDelta: vi.fn(),
}))
vi.mock('@/components/ReasonCodeDialog', () => ({ default: () => null }))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ accountId: '7' }),
  useNavigate: () => vi.fn(),
}))

/**
 * A failure total is not a diagnosis. Live, 119,600 failures were one real
 * bug plus two families describing states that had since stopped being true
 * — about six times the number that actually needed anybody.
 *
 * The survey rides inside the detail payload rather than arriving from its
 * own request. Fetched separately it drifted: the header read 382 failures
 * while the panel beside it read 383, because the two requests landed
 * seconds apart on a run producing failures continuously. Both were correct;
 * together they read as a bug.
 */
const survey = (over: Record<string, unknown> = {}) => ({
  accountId: 7,
  total: 119600,
  unclassified: 394,
  error: '',
  families: [
    {
      key: 'acl_no_account', count: 58642,
      label: 'share grants refused — the person had no account at the time',
      fix: 'resolvable now',
    },
    {
      key: 'gmail_invalid_label', count: 32967,
      label: 'messages rejected — label pointed at a deleted mailbox',
      fix: 'retried on the next migration',
    },
  ],
  ...over,
})

const detail = (over: Record<string, unknown> = {}) => ({
  accountId: 7,
  sourceDomain: 'a.com',
  targetDomain: 'b.com',
  running: false,
  error: '',
  progress: {
    users: 201, done: 200, running: 0, pending: 0, failed: 0, blocked: 0,
    items: 818000, itemsFailed: 119600, itemsSkipped: 0,
  },
  items: [], failures: [], users: [], failedUsers: [], skipped: [],
  repair: survey(),
  ...over,
})

describe('MigrationDetail repair panel', () => {
  beforeEach(() => {
    fetchMigrationDetail.mockReset()
    runRepair.mockReset()
  })

  it('breaks the failure total into named causes', async () => {
    fetchMigrationDetail.mockResolvedValue(detail())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('repair-panel')).toBeTruthy())
    expect(screen.getByTestId('repair-acl_no_account').textContent)
      .toContain('58,642')
    expect(screen.getByTestId('repair-gmail_invalid_label').textContent)
      .toContain('retried on the next migration')
  })

  it('shows what is not classified rather than hiding it', async () => {
    // A survey that only counts what it recognises reports a friendlier
    // number than the truth.
    fetchMigrationDetail.mockResolvedValue(detail())
    render(<MigrationDetail />)
    await waitFor(() =>
      expect(screen.getByTestId('repair-unclassified')).toBeTruthy())
    expect(screen.getByTestId('repair-unclassified').textContent).toContain('394')
  })

  it('agrees with the headline failure count', async () => {
    // The whole reason the survey moved into this payload: two totals on one
    // screen, read at different instants, disagreeing by one.
    fetchMigrationDetail.mockResolvedValue(detail({
      progress: {
        users: 201, done: 0, running: 48, pending: 153, failed: 0, blocked: 0,
        items: 54293, itemsFailed: 383, itemsSkipped: 55075,
      },
      repair: survey({ total: 383, unclassified: 25, families: [
        { key: 'acl_no_account', count: 85, label: 'no account at the time',
          fix: 'resolvable now' },
        { key: 'acl_quota', count: 273, label: 'rate limits',
          fix: 'checked against the target' },
      ] }),
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('repair-panel')).toBeTruthy())
    expect(screen.getByTestId('stat-itemsfailed').textContent).toContain('383')
    expect(screen.getByTestId('repair-panel').textContent).toContain('383')
  })

  it('offers the repair button when nothing is running', async () => {
    fetchMigrationDetail.mockResolvedValue(detail())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('run-repair')).toBeTruthy())
    expect(screen.getByTestId('run-repair').hasAttribute('disabled')).toBe(false)
  })

  it('disables it while a migration is running, and says why', async () => {
    // Repair runs automatically at the end; racing the run that is still
    // writing those rows is not useful.
    fetchMigrationDetail.mockResolvedValue(detail({ running: true }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('run-repair')).toBeTruthy())
    const btn = screen.getByTestId('run-repair')
    expect(btn.hasAttribute('disabled')).toBe(true)
    expect(btn.textContent).toContain('when the migration finishes')
  })

  it('shows no panel when there is nothing to repair', async () => {
    fetchMigrationDetail.mockResolvedValue(detail({
      repair: survey({ total: 0, families: [], unclassified: 0 }),
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    expect(screen.queryByTestId('repair-panel')).toBeNull()
  })

  it('survives the survey being absent from the payload', async () => {
    fetchMigrationDetail.mockResolvedValue(detail({ repair: undefined }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    expect(screen.queryByTestId('repair-panel')).toBeNull()
  })
})

describe('MigrationDetail flags broken folder shares', () => {
  beforeEach(() => { fetchMigrationDetail.mockReset() })

  it('calls out folder shares separately from the total', async () => {
    // Sharing is folder-derived, so a failed folder grant gates every file
    // inside it. 147 folders once accounted for 1,050 inaccessible files
    // while sitting in the same total as 142 single-file failures.
    fetchMigrationDetail.mockResolvedValue(detail({
      repair: survey({
        brokenFolders: { folders: 147, grants: 265, files_behind: 1050 },
      }),
    }))
    render(<MigrationDetail />)
    await waitFor(() =>
      expect(screen.getByTestId('broken-folders')).toBeTruthy())
    const t = screen.getByTestId('broken-folders').textContent || ''
    expect(t).toContain('147')
    expect(t).toContain('1,050')
    expect(t).toContain('folders first')
  })

  it('agrees in number with the folder count, not the file count', async () => {
    // "inside it" refers to the FOLDER. Keying the pronoun off the file
    // count produced "1 folder share failed, and 60 files inside them".
    fetchMigrationDetail.mockResolvedValue(detail({
      repair: survey({
        brokenFolders: { folders: 1, grants: 2, files_behind: 60 },
      }),
    }))
    render(<MigrationDetail />)
    await waitFor(() =>
      expect(screen.getByTestId('broken-folders')).toBeTruthy())
    const t = screen.getByTestId('broken-folders').textContent || ''
    expect(t).toContain('1 folder share failed')
    expect(t).toContain('inside it')
    expect(t).not.toContain('inside them')
  })

  it('says nothing when no folder share failed', async () => {
    fetchMigrationDetail.mockResolvedValue(detail({
      repair: survey({
        brokenFolders: { folders: 0, grants: 0, files_behind: 0 },
      }),
    }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('repair-panel')).toBeTruthy())
    expect(screen.queryByTestId('broken-folders')).toBeNull()
  })

  it('survives the field being absent entirely', async () => {
    fetchMigrationDetail.mockResolvedValue(detail())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('repair-panel')).toBeTruthy())
    expect(screen.queryByTestId('broken-folders')).toBeNull()
  })
})

describe('MigrationDetail reports what was skipped', () => {
  beforeEach(() => { fetchMigrationDetail.mockReset() })

  it('shows skipped items alongside migrated and failed', async () => {
    // 56,975 items on a live tenant were neither migrated nor failed, and
    // the page showed nothing between those two numbers.
    fetchMigrationDetail.mockResolvedValue(detail({
      progress: {
        users: 201, done: 201, running: 0, pending: 0, failed: 0, blocked: 0,
        items: 818000, itemsFailed: 422, itemsSkipped: 56975,
      },
    }))
    render(<MigrationDetail />)
    await waitFor(() =>
      expect(screen.getByTestId('stat-itemsskipped')).toBeTruthy())
    expect(screen.getByTestId('stat-itemsskipped').textContent)
      .toContain('56,975')
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

describe('MigrationDetail says how old its numbers are', () => {
  beforeEach(() => { fetchMigrationDetail.mockReset() })

  it('shows the age of a cached payload', async () => {
    // The payload is served from a 15s server cache. On a run moving 40
    // items a second that is a ~600-item gap against the ledger, and
    // unlabelled it reads as the counters being stuck.
    const twelveAgo = new Date(Date.now() - 12_000).toISOString()
    fetchMigrationDetail.mockResolvedValue(detail({ asOf: twelveAgo }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('as-of')).toBeTruthy())
    expect(screen.getByTestId('as-of').textContent).toMatch(/1[0-9]s ago/)
  })

  it('says "just now" for a fresh payload', async () => {
    fetchMigrationDetail.mockResolvedValue(
      detail({ asOf: new Date().toISOString() }))
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('as-of')).toBeTruthy())
    expect(screen.getByTestId('as-of').textContent).toContain('just now')
  })

  it('shows nothing when the server did not stamp it', async () => {
    fetchMigrationDetail.mockResolvedValue(detail())
    render(<MigrationDetail />)
    await waitFor(() => expect(screen.getByTestId('users-table')).toBeTruthy())
    expect(screen.queryByTestId('as-of')).toBeNull()
  })
})
