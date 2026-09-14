/**
 * Every seeder ability is reachable from the Jobs page, not just scale +
 * create-users. The seeder can seed subsets of users and services, fit to
 * licences, create until full, size mail/events, make oversized files and
 * shared drives, top up storage to a target, aim an external collaborator,
 * and choose the edge-case spread -- and until now the UI exposed three of
 * those. This proves the advanced controls actually flow through to runSeed.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Jobs from './Jobs'
import * as client from '@/api/client'

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return {
    ...actual,
    fetchJob: vi.fn().mockResolvedValue({ name: '', running: false, lines: [] }),
    fetchJobHistory: vi.fn().mockResolvedValue([]),
    fetchSeedScopes: vi.fn().mockResolvedValue({ capabilities: [] }),
    fetchCompletedJobs: vi.fn().mockResolvedValue([]),
    fetchQueue: vi.fn().mockResolvedValue({
      capacity: 2, running: [], waiting: [], recent: [] }),
    runSeed: vi.fn().mockResolvedValue({ ok: true }),
  }
})

vi.mock('@/api/controlPlane', () => ({
  fetchTenantConfigStatus: (side: 'source' | 'target') => Promise.resolve({
    side, domain: side === 'source' ? 'src.example' : 'tgt.example',
    adminEmail: `admin@${side}.example`, hasKey: true, clientId: '1', scopes: [],
  }),
  fetchVerifiedDomains: () => Promise.resolve({ domains: [] }),
  fetchFullSetupStatus: () => Promise.resolve({}),
  fetchDwdStatus: () => Promise.resolve({ caveats: [] }),
  fetchFleet: () => Promise.resolve([]),
  fetchActiveJobs: () => Promise.resolve([]),
  fetchMe: () => Promise.resolve({ is_superadmin: true, id: 1, seed_enabled: true }),
  startMigration: () => Promise.resolve({ ok: true, detail: '' }),
  stopJob: () => Promise.resolve({ ok: true }),
  getCpBase: () => '',
}))

vi.setConfig({ testTimeout: 20000 })

vi.mock('@/hooks/useRunningJobs', () => ({
  useRunningJobs: () => ({ jobs: [] }),
  jobKind: () => 'other',
  describeElapsed: () => '',
}))

const view = () => render(<MemoryRouter><Jobs /></MemoryRouter>)

const openAdvanced = async () => {
  view()
  fireEvent.click(await screen.findByTestId('tenant-card-source'))
  fireEvent.click(await screen.findByTestId('seed-advanced-toggle'))
  await screen.findByTestId('seed-advanced')
}

beforeEach(() => { vi.mocked(client.runSeed).mockClear() })

describe('the advanced seed options are all present', () => {
  it('exposes every ability the seeder has', async () => {
    await openAdvanced()
    for (const t of ['seed-users', 'seed-prefix', 'seed-all-users', 'seed-fit',
                     'seed-until-full', 'seed-mail', 'seed-events', 'seed-bigfile',
                     'seed-shared-drives', 'seed-workers', 'seed-target-gb',
                     'seed-topup', 'seed-external', 'seed-edge',
                     'seed-svc-drive', 'seed-svc-gmail', 'seed-svc-chat']) {
      expect(screen.getByTestId(t), t).toBeInTheDocument()
    }
  })

  it('is hidden until asked for', async () => {
    view()
    fireEvent.click(await screen.findByTestId('tenant-card-source'))
    await screen.findByTestId('seed-now')
    // unmountOnExit: the advanced fields are not even in the DOM until the
    // disclosure is opened, so the common case stays a two-click job.
    expect(screen.queryByTestId('seed-users')).toBeNull()
    expect(screen.getByTestId('seed-advanced-toggle')).toHaveTextContent('Advanced options')
  })
})

describe('the advanced options reach runSeed', () => {
  const seed = async () => {
    fireEvent.click(screen.getByTestId('seed-now'))
    fireEvent.change(await screen.findByLabelText('Reason Code'),
                     { target: { value: 'rehearsal corpus' } })
    fireEvent.change(screen.getByLabelText('type SEED to confirm'),
                     { target: { value: 'SEED' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    return (vi.mocked(client.runSeed).mock.calls[0]?.[4] ?? {}) as client.SeedOptions
  }

  it('passes the volume and target knobs through', async () => {
    await openAdvanced()
    fireEvent.change(screen.getByTestId('seed-mail'), { target: { value: '40' } })
    fireEvent.change(screen.getByTestId('seed-events'), { target: { value: '12' } })
    fireEvent.change(screen.getByTestId('seed-bigfile'), { target: { value: '25' } })
    fireEvent.change(screen.getByTestId('seed-shared-drives'), { target: { value: '3' } })
    fireEvent.change(screen.getByTestId('seed-external'),
                     { target: { value: 'you@gmail.com' } })
    const opts = await seed()
    expect(opts).toMatchObject({
      mail: '40', events: '12', bigFileMb: '25', sharedDrives: '3',
      externalEmail: 'you@gmail.com',
    })
  })

  it('sends --only when services are unchecked, and nothing when all stay on', async () => {
    await openAdvanced()
    fireEvent.click(screen.getByTestId('seed-svc-chat'))   // uncheck chat
    fireEvent.click(screen.getByTestId('seed-svc-tasks'))  // uncheck tasks
    const opts = await seed()
    expect(opts.only).toBe('drive,gmail,calendar,contacts')
  })

  it('omits only when every service stays selected', async () => {
    await openAdvanced()
    const opts = await seed()
    expect(opts.only).toBeUndefined()
  })

  it('only offers top-up once a storage target is set', async () => {
    await openAdvanced()
    expect(screen.getByTestId('seed-topup')).toBeDisabled()
    fireEvent.change(screen.getByTestId('seed-target-gb'), { target: { value: '30' } })
    expect(screen.getByTestId('seed-topup')).not.toBeDisabled()
  })
})
