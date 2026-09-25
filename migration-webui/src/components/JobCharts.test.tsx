/**
 * Charts for every tracked metric on seed and migrate jobs. jsdom cannot lay
 * out an SVG, so these check what can go wrong without a browser: that each
 * chart is present, that an empty series says why instead of drawing zero,
 * and that the storage chart exists only for a fill run.
 */
import { render, screen, within } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import MigrateMetricsCharts from './MigrateMetricsCharts'
import SeedMetricCharts from './SeedMetricCharts'
import RunningJobDetail from './RunningJobDetail'
import { parseSeedRun } from '@/utils/seedLog'
import type { MetricsSnapshot } from '@/api/controlPlane'

const fetchMyMetrics = vi.hoisted(() => vi.fn())
vi.mock('@/api/controlPlane', () => ({ fetchMyMetrics }))

const EMPTY = { accountId: 1, error: '', latest: null, operations: [], limiters: {}, history: [] }
const RICH = {
  ...EMPTY,
  history: [
    { recordedAt: '2026-09-25T10:00:00Z', requestsPerSec: 2, p95: 0.2, failures: 0 },
    { recordedAt: '2026-09-25T10:00:05Z', requestsPerSec: 3, p95: 0.3, failures: 1 },
  ],
  operations: [{ label: 'drive.files.create', calls: 9, retries: 1, failures: 0, p50: 0.1, p95: 0.4 }],
  limiters: { drive: { rate: 5, floor: 1, ceiling: 10, rejections: 2, backoffs: 1 } },
  volume: [{ itemType: 'file', status: 'SUCCESS', count: 9 }],
  throughput: { byDay: [{ day: '2026-09-25', items: 9, bytes: 1024 }], expectedItems: 20,
                remainingItems: 11 },
  transfer: { bytesToday: 1024, dailyCapBytes: 4096 },
  mappings: [{ type: 'file', count: 9 }],
  host: { cores: 4, userWorkers: 3, seedWorkers: 2 },
} as unknown as MetricsSnapshot

const frame = (t: string) => screen.getByTestId(`chart-${t}`)

beforeEach(() => fetchMyMetrics.mockReset())

describe('migrate charts', () => {
  it('says why a series is empty instead of drawing it as zero', () => {
    render(<MigrateMetricsCharts m={EMPTY as unknown as MetricsSnapshot} />)
    expect(within(frame('Requests per second')).getByText(/Needs two snapshots/)).toBeInTheDocument()
    expect(within(frame('Latency by operation')).getByText('No calls recorded yet.')).toBeInTheDocument()
    expect(within(frame('Items done and remaining')).getByText(/No expected total/)).toBeInTheDocument()
    expect(within(frame('Uploaded today against the daily cap')).getByText('No daily cap configured.'))
      .toBeInTheDocument()
  })

  it('draws every series it has data for', () => {
    render(<MigrateMetricsCharts m={RICH} />)
    for (const t of ['Requests per second', 'p95 latency', 'Latency by operation', 'Rate limiters',
                     'Work per day', 'Outcome by item type', 'Items done and remaining',
                     'Live mappings on the target', 'Workers against cores']) {
      expect(frame(t).textContent).not.toMatch(/Needs two|No calls|Nothing recorded|No expected|No mappings/)
    }
    // Retries exist, so that chart is not the "clean run" placeholder.
    expect(frame('Retries and failures by operation').textContent).not.toMatch(/clean run/)
  })
})

describe('seed charts', () => {
  const plain = parseSeedRun(['Seeding 2 users in x.com at scale \'small\'',
    '  [a@x.com] done in 10.0s: 4 files', '  [b@x.com] done in 20.0s: 6 files'])
  const fill = parseSeedRun(['  [a@x.com] top-up in 5.0s: 1.0GB -> 2.0GB (2 filler file(s))'])

  it('shows no storage chart for a run that filled nothing', () => {
    render(<SeedMetricCharts run={plain} />)
    expect(screen.queryByTestId('chart-Storage filled per user')).toBeNull()
    expect(frame('Slowest users')).toBeInTheDocument()
  })

  it('shows the storage chart, with what was added, for a fill run', () => {
    render(<SeedMetricCharts run={fill} />)
    expect(frame('Storage filled per user')).toHaveTextContent(/1 GB added across 1 user/)
  })
})

describe('a migrate job\'s detail', () => {
  it('pulls the migration\'s charts in, and not for other job kinds', async () => {
    fetchMyMetrics.mockResolvedValue(RICH)
    const job = { key: 'k', label: 'migrate', detail: 'x', pct: 10, kind: 'migrate' as const }
    const { unmount } = render(<RunningJobDetail job={job} onClose={() => {}} />)
    expect(await screen.findByTestId('migrate-charts')).toBeInTheDocument()
    unmount()
    render(<RunningJobDetail job={{ ...job, kind: 'reset' }} onClose={() => {}} />)
    expect(screen.queryByTestId('migrate-charts')).toBeNull()
  })
})
