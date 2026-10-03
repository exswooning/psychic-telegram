import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import RunMetricCards from './RunMetricCards'

const fetchMetricRuns = vi.fn()
vi.mock('@/api/controlPlane', () => ({ fetchMetricRuns: (...a: unknown[]) => fetchMetricRuns(...a) }))

describe('a card per run, titled with its domains', () => {
  it('shows the pair, the kind and the kept numbers', async () => {
    fetchMetricRuns.mockResolvedValue({ accountId: 3, runs: [
      { runKey: 'migrate:1:x', kind: 'migrate', sourceDomain: 'src.example', targetDomain: 'tgt.example',
        startedAt: '2026-10-03T10:23:16Z', updatedAt: '', calls: 12345, requests_per_sec: 9.5,
        peak_requests_per_sec: 58.6, p50: 210, p95: 1800, retries: 3, failures: 0, peak_rss_mb: 640,
        elapsed_sec: 1500 },
      { runKey: 'seed:2:y', kind: 'seed', sourceDomain: 'src.example', targetDomain: null,
        startedAt: '2026-10-03T09:41:54Z', updatedAt: '', calls: 1531 },
    ] })
    render(<RunMetricCards accountId={3} />)
    const card = await screen.findByTestId('run-card-migrate:1:x')
    expect(card).toHaveTextContent('src.example → tgt.example')
    expect(card).toHaveTextContent('12,345')
    expect(card).toHaveTextContent('58.6')
    expect(screen.getByTestId('run-card-seed:2:y')).toHaveTextContent('src.example')
  })
})
