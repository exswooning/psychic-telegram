/**
 * Wipe / Delete all users on the Jobs page's per-tenant cards.
 *
 * The actions already existed on Mission Control. Putting them here too is
 * only an improvement if they are the SAME actions -- same typed-domain
 * gate, same endpoint, same wording about what survives. A second, weaker
 * confirm path in front of "delete every account in this tenant" would be
 * worse than not having the button.
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
    fetchJobHistory: vi.fn().mockResolvedValue([]),
    fetchSeedScopes: vi.fn().mockResolvedValue({ capabilities: [] }),
    fetchCompletedJobs: vi.fn(),
    fetchQueue: vi.fn().mockResolvedValue({
      capacity: 2, running: [], waiting: [], recent: [] }),
    removeTenantSetup: vi.fn().mockResolvedValue({ ok: true }),
  }
})

// A plain factory, not importActual: controlPlane reads localStorage at
// module scope, and a mock factory is hoisted above the jsdom setup that
// provides it. Listing what Jobs actually uses is also the honest scope --
// anything it reaches for that is not here is a real coupling this test
// should be told about.
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
  fetchMe: () => Promise.resolve({ is_superadmin: true, id: 1,
                                   seed_enabled: true }),
  startMigration: () => Promise.resolve({ ok: true, detail: '' }),
  stopJob: () => Promise.resolve({ ok: true }),
  getCpBase: () => '',
}))

// This test is about the tenant cards, not about live jobs. Mocking the
// hook keeps it from having to stand up every endpoint useRunningJobs
// polls, and keeps a change there from failing a test that is not about it.
vi.mock('@/hooks/useRunningJobs', () => ({
  useRunningJobs: () => ({ jobs: [] }),
  jobKind: () => 'other',
  describeElapsed: () => '',
}))

const RUN = {
  name: 'seed', rc: 0, started: 1_700_000_000, finished: 1_700_003_600,
  elapsed: 3600, lineCount: 10, fromTranscript: true,
}

beforeEach(() => {
  vi.mocked(client.fetchCompletedJobs).mockResolvedValue([RUN as never])
  vi.mocked(client.removeTenantSetup).mockClear()
})

const view = () => render(<MemoryRouter><Jobs /></MemoryRouter>)

describe('the buttons are on the tenant card', () => {
  it('offers a wipe', async () => {
    view()
    expect(await screen.findByTestId('wipe-runs-src.example')).toBeInTheDocument()
  })

  it('offers delete all users', async () => {
    view()
    expect(await screen.findByTestId('delete-users-runs-src.example'))
      .toBeInTheDocument()
  })

  it('offers neither for a tenant that is no longer configured', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue(
      [{ ...RUN, name: 'reset target' } as never])
    const { container } = view()
    await screen.findByTestId('domain-runs-tgt.example')
    // configured target exists here, so use a run naming a tenant with no
    // configuration at all
    expect(container.querySelector('[data-testid^="wipe-runs-target ("]')).toBeNull()
  })
})

describe('the gate is the same one Mission Control uses', () => {
  it('asks for the domain to be typed', async () => {
    view()
    fireEvent.click(await screen.findByTestId('wipe-runs-src.example'))
    expect(await screen.findByTestId('confirm-domain')).toBeInTheDocument()
  })

  it('will not act until the domain matches', async () => {
    view()
    fireEvent.click(await screen.findByTestId('wipe-runs-src.example'))
    const go = await screen.findByTestId('confirm-act')
    expect(go).toBeDisabled()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'tgt.example' } })
    expect(go).toBeDisabled()
  })

  it('acts once it does', async () => {
    view()
    fireEvent.click(await screen.findByTestId('wipe-runs-src.example'))
    fireEvent.change(await screen.findByTestId('confirm-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByTestId('confirm-act'))
    await waitFor(() => expect(client.removeTenantSetup)
      .toHaveBeenCalledWith('source', 'src.example', '', 'wipe'))
  })

  it('does not ask for a password a wipe will never use', async () => {
    view()
    fireEvent.click(await screen.findByTestId('wipe-runs-src.example'))
    await screen.findByTestId('confirm-domain')
    expect(screen.queryByTestId('admin-password')).toBeNull()
  })
})

describe('it sends the right tenant', () => {
  it('a target card acts on the target', async () => {
    vi.mocked(client.fetchCompletedJobs).mockResolvedValue(
      [{ ...RUN, name: 'reset target' } as never])
    view()
    fireEvent.click(await screen.findByTestId('delete-users-runs-tgt.example'))
    fireEvent.change(await screen.findByTestId('confirm-domain'),
                     { target: { value: 'tgt.example' } })
    fireEvent.click(screen.getByTestId('confirm-act'))
    await waitFor(() => expect(client.removeTenantSetup)
      .toHaveBeenCalledWith('target', 'tgt.example', '', 'delete_users'))
  })
})

describe('reaching for a button does not toggle the card', () => {
  // MUI keeps a collapsed Collapse's children MOUNTED and animates the
  // height, so textContent is identical either way -- asserting on it
  // passed with the stopPropagation removed, which is a test that proves
  // nothing. The hidden class is the thing that actually differs.
  const collapse = (card: HTMLElement) =>
    card.querySelector('.MuiCollapse-root')!

  it('the header still expands the run list', async () => {
    view()
    const card = await screen.findByTestId('domain-runs-src.example')
    expect(collapse(card).className).toMatch(/MuiCollapse-hidden/)
    // Within the card: the domain name also appears on the side-job cards
    // further down the page.
    fireEvent.click(within(card).getByText('src.example'))
    await waitFor(() =>
      expect(collapse(card).className).not.toMatch(/MuiCollapse-hidden/))
  })

  it('but pressing Wipe does not', async () => {
    view()
    const card = await screen.findByTestId('domain-runs-src.example')
    fireEvent.click(screen.getByTestId('wipe-runs-src.example'))
    expect(await screen.findByTestId('confirm-domain')).toBeInTheDocument()
    expect(collapse(card).className).toMatch(/MuiCollapse-hidden/)
  })

  it('nor pressing Delete users', async () => {
    view()
    const card = await screen.findByTestId('domain-runs-src.example')
    fireEvent.click(screen.getByTestId('delete-users-runs-src.example'))
    expect(await screen.findByTestId('confirm-domain')).toBeInTheDocument()
    expect(collapse(card).className).toMatch(/MuiCollapse-hidden/)
  })
})
