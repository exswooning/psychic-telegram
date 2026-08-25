import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import Metrics from './Metrics'

const fetchMetrics = vi.fn()
const fetchMyMetrics = vi.fn()

vi.mock('@/api/controlPlane', () => ({
  fetchMetrics: (...a: unknown[]) => fetchMetrics(...a),
  fetchMyMetrics: (...a: unknown[]) => fetchMyMetrics(...a),
}))
let params: Record<string, string> = { accountId: '7' }
vi.mock('react-router-dom', () => ({ useParams: () => params }))

/**
 * The page exists because these numbers were previously read from the wrong
 * process: webui_spa called METRICS.snapshot() inside api_server, which
 * issues no Drive calls, so an empty reservoir rendered as the run's
 * performance. Anything that makes "no data" look like "fast and idle" is
 * the specific failure to guard against.
 */
const snap = (over: Record<string, unknown> = {}) => ({
  accountId: 7,
  error: '',
  latest: {
    recordedAt: '2026-08-22T04:00:00Z',
    elapsedSec: 600, calls: 12000, workers: 16,
    requestsPerSec: 20, requestsPerSecPerWorker: 1.25,
    p50: 0.12, p95: 0.9, p99: 2.5, retries: 4, failures: 0,
  },
  operations: [
    { label: 'drive.files.copy', calls: 900, retries: 2, failures: 1, p50: 0.5, p95: 2.2 },
    { label: 'drive.files.list', calls: 4000, retries: 0, failures: 0, p50: 0.05, p95: 0.2 },
  ],
  limiters: {},
  history: [],
  ...over,
})

describe('Metrics', () => {
  beforeEach(() => {
    fetchMetrics.mockReset(); fetchMyMetrics.mockReset()
    params = { accountId: '7' }
  })

  it('shows the headline throughput and latency', async () => {
    fetchMetrics.mockResolvedValue(snap())
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('metric-rps')).toBeTruthy())
    expect(screen.getByTestId('metric-rps').textContent).toContain('20.0')
    expect(screen.getByTestId('metric-p95').textContent).toContain('900ms')
    expect(screen.getByTestId('metric-workers').textContent).toContain('16')
  })

  it('renders sub-second latency in ms and longer in seconds', async () => {
    fetchMetrics.mockResolvedValue(snap())
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('metric-p50')).toBeTruthy())
    expect(screen.getByTestId('metric-p50').textContent).toContain('120ms')
    expect(screen.getByTestId('metric-p99').textContent).toContain('2.50s')
  })

  it('orders operations slowest first', async () => {
    fetchMetrics.mockResolvedValue(snap())
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('operations')).toBeTruthy())
    const rows = screen.getAllByTestId(/^op-/)
    expect(rows[0].getAttribute('data-testid')).toBe('op-drive.files.copy')
  })

  it('says plainly when nothing has been recorded', async () => {
    // The whole point: an empty reservoir must not render as a healthy,
    // idle run. It reads as "no metrics yet", not as zero latency.
    fetchMetrics.mockResolvedValue(
      snap({ latest: null, operations: [], error: 'no metrics recorded yet' }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('metrics-empty')).toBeTruthy())
    expect(screen.queryByTestId('metric-rps')).toBeNull()
  })

  it('shows each project limiter separately', async () => {
    // Source and target are different GCP projects, metered separately by
    // Google. One combined number would hide which side is throttled.
    fetchMetrics.mockResolvedValue(snap({
      limiters: {
        source: { rate: 1200, floor: 5, ceiling: 1200, rejections: 0, backoffs: 0 },
        target: { rate: 45.9, floor: 5, ceiling: 1200, rejections: 6, backoffs: 6 },
      },
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('limiter-source')).toBeTruthy())
    expect(screen.getByTestId('limiter-target').textContent).toContain('45.9/s')
    expect(screen.getByTestId('limiter-target').textContent).toContain('6 quota rejections')
  })

  it('flags a limiter pinned at its ceiling as no longer the binding limit', async () => {
    fetchMetrics.mockResolvedValue(snap({
      limiters: {
        source: { rate: 1200, floor: 5, ceiling: 1200, rejections: 0, backoffs: 0 },
      },
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('limiter-source')).toBeTruthy())
    expect(screen.getByTestId('limiter-source').textContent)
      .toContain('not the binding limit')
  })

  it('surfaces a fetch error rather than rendering an empty page', async () => {
    fetchMetrics.mockRejectedValue(new Error('control plane unreachable'))
    render(<Metrics />)
    await waitFor(() =>
      expect(screen.getByText(/control plane unreachable/)).toBeTruthy())
  })
})

describe('Metrics from the sidebar (no account in context)', () => {
  beforeEach(() => { fetchMetrics.mockReset(); fetchMyMetrics.mockReset() })

  it('asks the server to resolve the account', async () => {
    // The sidebar entry has no migration in context. Making someone find
    // the run first is a step between them and the answer.
    params = {}
    fetchMyMetrics.mockResolvedValue(snap())
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('metric-rps')).toBeTruthy())
    expect(fetchMyMetrics).toHaveBeenCalled()
    expect(fetchMetrics).not.toHaveBeenCalled()
  })

  it('uses the scoped endpoint when opened from a migration', async () => {
    params = { accountId: '7' }
    fetchMetrics.mockResolvedValue(snap())
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('metric-rps')).toBeTruthy())
    expect(fetchMetrics).toHaveBeenCalledWith(7)
    expect(fetchMyMetrics).not.toHaveBeenCalled()
  })
})

describe('Metrics covers more than API latency', () => {
  beforeEach(() => {
    fetchMetrics.mockReset(); fetchMyMetrics.mockReset()
    params = { accountId: '7' }
  })

  it('shows item counts per type and outcome without averaging them', async () => {
    // DONE, FAILED, SKIPPED and BLOCKED coexist and mean different things.
    // A single "94% complete" hides all four.
    fetchMetrics.mockResolvedValue(snap({
      volume: [
        { itemType: 'file', status: 'SUCCESS', count: 210456 },
        { itemType: 'acl', status: 'FAILED', count: 271330 },
        { itemType: 'file', status: 'SKIPPED_EXPORT_TOO_LARGE', count: 12 },
      ],
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('volume')).toBeTruthy())
    expect(screen.getByTestId('vol-file-SUCCESS').textContent).toContain('210,456')
    expect(screen.getByTestId('vol-acl-FAILED').textContent).toContain('271,330')
  })

  it('does not colour a SKIPPED outcome as a failure', async () => {
    // Skips are decisions, not errors. Colouring them red is how a clean
    // run teaches people to ignore red.
    fetchMetrics.mockResolvedValue(snap({
      volume: [{ itemType: 'file', status: 'SKIPPED_UNEXPORTABLE', count: 5 }],
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('volume')).toBeTruthy())
    const row = screen.getByTestId('vol-file-SKIPPED_UNEXPORTABLE')
    expect(row.querySelector('.MuiChip-colorError')).toBeNull()
  })

  it('shows transfer against the daily cap', async () => {
    fetchMetrics.mockResolvedValue(snap({
      transfer: { bytesToday: 100 * 1024 ** 3, dailyCapBytes: 750 * 1024 ** 3 },
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('transfer')).toBeTruthy())
    expect(screen.getByTestId('metric-bytes').textContent).toContain('100.0 GB')
    expect(screen.getByTestId('metric-capleft').textContent).toContain('650.0 GB')
  })

  it('shows host capacity and the reason the worker count was chosen', async () => {
    fetchMetrics.mockResolvedValue(snap({
      host: {
        cores: 2, ramTotalGb: 3.7, ramUsableGb: 2.6, swapFraction: 0,
        underMemoryPressure: false, userWorkers: 16, seedWorkers: 26,
        mbPerWorker: 64, reason: 'capped at 16; past this Google quotas bind',
      },
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('host')).toBeTruthy())
    expect(screen.getByTestId('metric-uworkers').textContent).toContain('16')
    expect(screen.getByTestId('host-reason').textContent).toContain('capped at 16')
  })

  it('shows live mappings separately from the audit history', async () => {
    // audit_log records every attempt ever made; id_mapping records what
    // currently exists. They disagree exactly when the target has lost
    // items the ledger still claims.
    fetchMetrics.mockResolvedValue(snap({
      mappings: [{ type: 'file', count: 51499 }],
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('mappings')).toBeTruthy())
    expect(screen.getByTestId('map-file').textContent).toContain('51,499')
  })

  it('omits sections the server did not send rather than showing zeros', async () => {
    fetchMetrics.mockResolvedValue(snap())
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('metric-rps')).toBeTruthy())
    expect(screen.queryByTestId('volume')).toBeNull()
    expect(screen.queryByTestId('host')).toBeNull()
  })
})

describe('Metrics discloses a changed sharing model', () => {
  beforeEach(() => {
    fetchMetrics.mockReset(); fetchMyMetrics.mockReset()
    params = { accountId: '7' }
  })

  it('says so when sharing became folder-derived', async () => {
    // The engine measured the corpus and stopped recreating folder-inherited
    // grants per file. Right call, ~50x less work -- and it changes what the
    // migration preserves, so it must not live only in one log line.
    fetchMetrics.mockResolvedValue(snap({
      inheritedAcls: { files: 50, inherited: 10050, disabled: true, density: 201 },
    }))
    render(<Metrics />)
    await waitFor(() =>
      expect(screen.getByTestId('inherited-acls')).toBeTruthy())
    const t = screen.getByTestId('inherited-acls').textContent || ''
    expect(t).toContain('201')
    expect(t).toContain('moved out of the folder')
  })

  it('says nothing when per-file grants were kept', async () => {
    fetchMetrics.mockResolvedValue(snap({
      inheritedAcls: { files: 50, inherited: 100, disabled: false },
    }))
    render(<Metrics />)
    await waitFor(() => expect(screen.getByTestId('metric-rps')).toBeTruthy())
    expect(screen.queryByTestId('inherited-acls')).toBeNull()
  })
})
