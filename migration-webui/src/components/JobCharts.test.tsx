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
  limiterHistory: { target: [
    { t: 1000, rate: 40, kind: 'probe' }, { t: 1020, rate: 44, kind: 'probe' },
    { t: 1040, rate: 31, kind: 'backoff' }, { t: 1160, rate: 34, kind: 'backoff' }] },
  host: { cores: 4, userWorkers: 3, seedWorkers: 2 },
} as unknown as MetricsSnapshot

const frame = (t: string) => screen.getByTestId(`chart-${t}`)

beforeEach(() => fetchMyMetrics.mockReset())

describe('migrate charts', () => {
  it('says why a series is empty instead of drawing it as zero', () => {
    render(<MigrateMetricsCharts m={EMPTY as unknown as MetricsSnapshot} />)
    expect(within(frame('Requests per second')).getByText(/Needs two snapshots/)).toBeInTheDocument()
    expect(within(frame('Rate limiter sawtooth')).getByText(/Needs limiter snapshots/)).toBeInTheDocument()
    expect(within(frame('Latency by operation')).getByText('No calls recorded yet.')).toBeInTheDocument()
    expect(within(frame('Items done and remaining')).getByText(/No expected total/)).toBeInTheDocument()
    expect(within(frame('Uploaded today against the daily cap')).getByText('No daily cap configured.'))
      .toBeInTheDocument()
  })

  it('gives each limiter its own sawtooth, so a 1,200/s bucket cannot flatten a 45/s one', () => {
    render(<MigrateMetricsCharts m={{ ...RICH, limiterHistory: {
      source: [{ t: 1, rate: 1200, kind: 'sample' }, { t: 2, rate: 1200, kind: 'sample' }],
      target: [{ t: 1, rate: 40, kind: 'probe' }, { t: 2, rate: 30, kind: 'backoff' }] } } as MetricsSnapshot} />)
    expect(frame('Sawtooth · source')).toHaveTextContent('0 pushbacks')
    expect(frame('Sawtooth · target')).toHaveTextContent('1 pushback · 30–40 calls/s')
  })

  it('draws every series it has data for', () => {
    render(<MigrateMetricsCharts m={RICH} />)
    for (const t of ['Requests per second', 'p95 latency', 'Latency by operation', 'Rate limiters',
                     'Work per day', 'Outcome by item type', 'Items done and remaining',
                     'Live mappings on the target', 'Workers against cores']) {
      expect(frame(t).textContent).not.toMatch(/Needs two|No calls|Nothing recorded|No expected|No mappings/)
    }
    // The sawtooth names what it shows: pushbacks, their spacing, the range.
    // Each limiter gets its own chart, and the frame names what it shows:
    // pushbacks, their spacing, the range.
    expect(frame('Sawtooth · target')).toHaveTextContent('2 pushbacks, one every 120s · 31–44 calls/s')
    expect(screen.queryByTestId('chart-Rate limiter sawtooth')).toBeNull()
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

  it('draws upload rate and retries for a run that printed the heartbeats', () => {
    const beat = (m: number, up: string, n: number) =>
      `  ... still topping up: 0/300 users done after ${m}m00s (12 in flight) -- ${up} GB uploaded of 383 GB planned, 9.5 req/s, ${n} retried (0.4%)`
    render(<SeedMetricCharts run={parseSeedRun([beat(1, '5', 2), beat(2, '11', 9), beat(3, '15', 9)])} />)
    expect(frame('Upload rate')).not.toHaveTextContent(/Needs three/)
    expect(frame('Request rate and retries')).not.toHaveTextContent(/Needs two heartbeats/)
    // Said in the chart, not left to be assumed: seeds have no limiter to sawtooth.
    expect(frame('Request rate and retries')).toHaveTextContent('no limiter sawtooth')
  })

  it('drops the items chart for a run whose finished users wrote no items', () => {
    // A fill writes filler files only, so the cumulative line is flat at zero.
    render(<SeedMetricCharts run={parseSeedRun([
      '  [a@x.com] top-up in 5.0s: 1.0GB -> 2.0GB (2 filler file(s))',
      '  [b@x.com] top-up in 6.0s: 1.0GB -> 2.0GB (2 filler file(s))'])} />)
    expect(screen.queryByTestId('chart-Items written as users finish')).toBeNull()
  })

  it('keeps the items chart for a seed that wrote items', () => {
    render(<SeedMetricCharts run={plain} />)
    expect(frame('Items written as users finish')).toBeInTheDocument()
  })

  it('says why the retry chart is empty for a run that predates the figures', () => {
    render(<SeedMetricCharts run={plain} />)
    expect(frame('Request rate and retries')).toHaveTextContent(/runs started after this update/)
    expect(screen.queryByTestId('chart-Upload rate')).toBeNull()
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
