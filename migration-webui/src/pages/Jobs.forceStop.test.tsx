/**
 * The engine reads its stop flag between items, so one file with a hundred slow
 * grants kept a "stopped" quick migration alive for an hour -- and the Jobs page
 * had no way to end it. The second press on the same run has to kill it.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Jobs from './Jobs'
import * as client from '@/api/client'

const h = vi.hoisted(() => ({ stop: vi.fn(), jobs: [] as unknown[] }))
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
  fetchCompletedJobsAcrossAccounts: () => Promise.resolve([]),
  fetchJobHistoryFor: () => Promise.resolve(null),
}))
vi.mock('@/hooks/useRunningJobs', () => ({
  useRunningJobs: () => ({ jobs: h.jobs }),
  jobKind: () => 'migrate',
  describeElapsed: () => '',
}))

const view = () => render(<MemoryRouter><Jobs /></MemoryRouter>)

const press = async () => {
  fireEvent.click(await screen.findByTestId('stop-fleet-1'))
  fireEvent.change(await screen.findByLabelText('Reason Code'), { target: { value: 'still running' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
}

beforeEach(() => {
  h.stop.mockReset().mockResolvedValue(undefined)
  h.jobs = [{
    key: 'fleet-1', kind: 'migrate', label: 'migrate', detail: '', pct: null, stop: h.stop,
  }]
})

describe('stopping a run that took the stop and kept going', () => {
  it('the first press is a plain stop', async () => {
    view()
    await press()
    expect(h.stop).toHaveBeenCalledTimes(1)
    expect(h.stop.mock.calls[0][1]).toBe(false)
  })

  it('the second press on the same run is a force stop', async () => {
    view()
    await press()
    fireEvent.click(await screen.findByTestId('stop-fleet-1'))
    expect(await screen.findByText(/Force stop migrate/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'still running' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(h.stop).toHaveBeenCalledTimes(2))
    expect(h.stop.mock.calls[1][1]).toBe(true)
  })
})
