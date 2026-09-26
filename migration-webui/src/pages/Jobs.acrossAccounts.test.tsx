/**
 * A job watched running must not vanish when it finishes. The running list is
 * global, so an operator watches another account's twelve-hour seed; finished
 * runs were listed per signed-in account, so the moment it ended it fell into
 * a list nobody was looking at and the page said "nothing running".
 */
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Jobs from './Jobs'
import * as client from '@/api/client'
import { needsAttention } from '@/utils/groupRuns'

const cp = vi.hoisted(() => ({ across: vi.fn(), historyFor: vi.fn() }))
vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return {
    ...actual,
    fetchJob: vi.fn().mockResolvedValue({ name: '', running: false, lines: [] }),
    fetchJobHistory: vi.fn().mockResolvedValue(null),
    fetchSeedScopes: vi.fn().mockResolvedValue({ capabilities: [] }),
    fetchCompletedJobs: vi.fn().mockResolvedValue([]),
    fetchQueue: vi.fn().mockResolvedValue({ capacity: 2, running: [], waiting: [], recent: [] }),
  }
})
vi.mock('@/api/controlPlane', () => ({
  fetchDomainGuardStatus: () => Promise.resolve({ domain: 'x', protected: false }),
  // The SIGNED-IN account (3) has no source tenant at all.
  fetchTenantConfigStatus: () => Promise.resolve(null),
  fetchVerifiedDomains: () => Promise.resolve({ domains: [] }),
  fetchFullSetupStatus: () => Promise.resolve({}),
  fetchDwdStatus: () => Promise.resolve({ caveats: [] }),
  fetchFleet: () => Promise.resolve([]),
  fetchActiveJobs: () => Promise.resolve([]),
  fetchMe: () => Promise.resolve({ is_superadmin: true, id: 3, seed_enabled: true }),
  startMigration: () => Promise.resolve({ ok: true, detail: '' }),
  stopJob: () => Promise.resolve({ ok: true }),
  getCpBase: () => '',
  fetchCompletedJobsAcrossAccounts: cp.across,
  fetchJobHistoryFor: cp.historyFor,
}))
vi.mock('@/hooks/useRunningJobs', () => ({
  useRunningJobs: () => ({ jobs: [] }),
  jobKind: () => 'seed',
  describeElapsed: () => '',
}))

const run = (over = {}) => ({
  runId: 'seed.300', name: 'seed', rc: 0, started: 1, finished: 300, elapsed: 43376, lineCount: 500,
  fromTranscript: false, retryable: true, accountId: 2,
  sourceDomain: 'source.sarafgloabalexim.com', targetDomain: 'target.sarafgloabalexim.com', ...over,
})
const view = () => render(<MemoryRouter><Jobs /></MemoryRouter>)
const fetchMock = vi.fn()

beforeEach(() => {
  cp.across.mockReset().mockResolvedValue([run()])
  cp.historyFor.mockReset().mockResolvedValue(null)
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

describe('finished runs across accounts', () => {
  it('shows the seed that finished on ANOTHER account, under that account\'s own tenant', async () => {
    view()
    const group = await screen.findByTestId('completed-jobs')
    expect(group).toHaveTextContent('source.sarafgloabalexim.com')
    expect(group).not.toHaveTextContent('source (not configured)')
  })

  it('does not let a good run on one account clear a failure on another', () => {
    const bad = run({ accountId: 7, rc: 1, finished: 100, runId: 'seed.100' })
    const good = run({ accountId: 2, rc: 0, finished: 300 })
    expect(needsAttention([good, bad]).map((d) => d.accountId)).toEqual([7])
  })

  it('still supersedes an old failure by a later good run on the SAME account', () => {
    const bad = run({ rc: 1, finished: 100, runId: 'seed.100' })
    expect(needsAttention([run({ rc: 0, finished: 300 }), bad])).toEqual([])
  })

  it('opens the transcript from the account that ran it, not the signed-in one', async () => {
    cp.historyFor.mockResolvedValue({ name: 'seed', rc: 0, started: 1, finished: 300, elapsed: 1, lines: ['Topped up 300/300 user(s).'] })
    view()
    const group = await screen.findByTestId('completed-jobs')
    fireEvent.click(within(group).getByText('seed'))
    await waitFor(() => expect(cp.historyFor).toHaveBeenCalledWith(2, 'seed.300'))
    // (The page's own status read calls fetchJobHistory('seed') on mount; what must not
    // happen is reading THIS run from the signed-in account.)
    expect(client.fetchJobHistory).not.toHaveBeenCalledWith('seed', 'seed.300')
  })

  it('replays a retry against the account that ran it, whatever the stored request said', async () => {
    /* A stored request that never named an account would resolve to whoever is
       signed in (account 3, which has no tenant) and seed the wrong place. */
    cp.across.mockResolvedValue([run({ rc: 1, finished: 100, runId: 'seed.100' })])
    cp.historyFor.mockResolvedValue({ name: 'seed', rc: 1, started: 1, finished: 100, elapsed: 1, lines: [],
                                      retry: { path: '/api/seed', body: { confirm_domain: 'source.sarafgloabalexim.com' } } })
    fetchMock.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: false }) })
    view()
    fireEvent.click(await screen.findByTestId('retry-attention-seed.100'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/seed')
    expect(JSON.parse(init.body)).toMatchObject({ confirm_domain: 'source.sarafgloabalexim.com', account_id: 2 })
  })

  it('falls back to this account\'s own list if the cross-account read fails', async () => {
    cp.across.mockRejectedValue(new Error('HTTP 500'))
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue([
      { runId: 'seed.9', name: 'seed', rc: 0, finished: 9, lineCount: 1, fromTranscript: false }])
    view()
    expect(await screen.findByTestId('completed-jobs')).toHaveTextContent('seed')
  })
})
