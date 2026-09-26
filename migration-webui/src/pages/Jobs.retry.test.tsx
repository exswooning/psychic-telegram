/**
 * A killed or failed run has to be visible without opening anything, and
 * retrying it has to replay the request it was started with -- not guess at
 * one. Runs from before requests were recorded show the button disabled and
 * say why, rather than hiding it or offering something that cannot work.
 */
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Jobs from './Jobs'
import * as client from '@/api/client'

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return {
    ...actual,
    fetchJob: vi.fn().mockResolvedValue({ name: '', running: false, lines: [] }),
    fetchJobHistory: vi.fn(),
    fetchSeedScopes: vi.fn().mockResolvedValue({ capabilities: [] }),
    fetchCompletedJobs: vi.fn(),
    fetchQueue: vi.fn().mockResolvedValue({ capacity: 2, running: [], waiting: [], recent: [] }),
  }
})
vi.mock('@/api/controlPlane', () => ({
  fetchDomainGuardStatus: () => Promise.resolve({ domain: 'x', protected: false }),
  fetchTenantConfigStatus: (side: 'source' | 'target') => Promise.resolve({
    side, domain: side === 'source' ? 'src.example' : 'tgt.example',
    adminEmail: 'a@x', hasKey: true, clientId: '1', scopes: [] }),
  fetchVerifiedDomains: () => Promise.resolve({ domains: [] }),
  fetchFullSetupStatus: () => Promise.resolve({}),
  fetchDwdStatus: () => Promise.resolve({ caveats: [] }),
  fetchFleet: () => Promise.resolve([]),
  fetchActiveJobs: () => Promise.resolve([]),
  fetchMe: () => Promise.resolve({ is_superadmin: true, id: 1, seed_enabled: true }),
  // The cross-account list is unavailable here, so the page falls back to this
  // account's own -- which is what these tests are about.
  fetchCompletedJobsAcrossAccounts: () => Promise.reject(new Error('not in this test')),
  fetchJobHistoryFor: () => Promise.resolve(null),
  startMigration: () => Promise.resolve({ ok: true, detail: '' }),
  stopJob: () => Promise.resolve({ ok: true }),
  getCpBase: () => '',
}))
vi.mock('@/hooks/useRunningJobs', () => ({
  useRunningJobs: () => ({ jobs: [] }),
  jobKind: () => 'seed',
  describeElapsed: () => '',
}))

const run = (over = {}) => ({
  runId: 'seed.100', name: 'seed', rc: -15, started: 1, finished: 100, elapsed: 90,
  lineCount: 40, fromTranscript: false, retryable: true, ...over,
})
const view = () => render(<MemoryRouter><Jobs /></MemoryRouter>)
const fetchMock = vi.fn()
/** The history endpoint hands back `retry`; the retry itself gets `after`. */
const answer = (retry: { path: string; body: object }, after: object) =>
  fetchMock.mockImplementation((url: string) => Promise.resolve({
    ok: true, json: () => Promise.resolve(url.startsWith('/api/job_history')
      ? { result: { name: 'seed', rc: -15, started: 1, finished: 100, elapsed: 90, lines: [], retry } }
      : after),
  }))

beforeEach(() => {
  vi.mocked(client.fetchCompletedJobs).mockReset()
  // mockReset() drops every implementation, so it resolves to nothing again --
  // and Jobs.tsx chains .catch() on it. No history is null, as the real one is.
  vi.mocked(client.fetchJobHistory).mockReset().mockResolvedValue(null)
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

describe('killed runs on the Jobs page', () => {
  it('shows a killed run up front as "stopped", not a red "exit -15"', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue([run()])
    view()
    const strip = await screen.findByTestId('attention-jobs')
    expect(within(strip).getByText('stopped')).toBeInTheDocument()
    expect(within(strip).queryByText(/exit -15/)).toBeNull()
  })

  it('does not list a failure that a later good run has superseded', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue([
      run({ runId: 'seed.200', rc: 0, finished: 200 }), run()])
    view()
    await screen.findByTestId('completed-jobs')
    expect(screen.queryByTestId('attention-jobs')).toBeNull()
  })

  it('replays the recorded request when Retry is pressed', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue([run()])
    answer({ path: '/api/seed', body: { confirm_domain: 'src.example', fill_percent: 10 } },
           { ok: true, queued: false })
    view()
    fireEvent.click(await screen.findByTestId('retry-attention-seed.100'))
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => c[0] === '/api/seed')).toBe(true))
    const [, init] = fetchMock.mock.calls.find((c) => c[0] === '/api/seed')!
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ confirm_domain: 'src.example', fill_percent: 10 })
    expect(await screen.findByText(/seed started again/)).toBeInTheDocument()
  })

  it('shows Retry disabled for a run that predates recorded requests', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue([run({ retryable: false })])
    view()
    expect(await screen.findByTestId('retry-attention-seed.100')).toBeDisabled()
  })

  it('reports a refusal from the server instead of pretending it started', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue([run()])
    answer({ path: '/api/seed', body: {} }, { ok: false, error: 'seed is still running' })
    view()
    fireEvent.click(await screen.findByTestId('retry-attention-seed.100'))
    expect(await screen.findByText('seed is still running')).toBeInTheDocument()
  })

  it('refuses to replay anything but a seed request', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue([run()])
    answer({ path: '/api/reset_target', body: { confirm_domain: 'x' } }, { ok: true })
    view()
    fireEvent.click(await screen.findByTestId('retry-attention-seed.100'))
    expect(await screen.findByText(/did not record how it was started/)).toBeInTheDocument()
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/reset_target')).toBe(false)
  })
})
