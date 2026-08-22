import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import Metrics from './Metrics'

const fetchMetrics = vi.fn()

vi.mock('@/api/controlPlane', () => ({
  fetchMetrics: (...a: unknown[]) => fetchMetrics(...a),
}))
vi.mock('react-router-dom', () => ({ useParams: () => ({ accountId: '7' }) }))

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
  beforeEach(() => { fetchMetrics.mockReset() })

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
