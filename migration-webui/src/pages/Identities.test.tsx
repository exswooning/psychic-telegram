/**
 * The identity page maps source->target users, but that mapping is only
 * meaningful once the domains it maps between are actually set up. The
 * scoped-domain cards put that context on the page: which side, which admin,
 * and how many delegation scopes are live.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Identities from './Identities'

const verifiedDomains = vi.fn()
const tenantInventory = vi.fn()
const allDomains = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchVerifiedDomains: () => verifiedDomains(),
  fetchTenantInventory: (...a: unknown[]) => tenantInventory(...a),
  fetchAllDomains: () => allDomains(),
}))
vi.mock('@/api/client', () => ({
  fetchActions: () => Promise.resolve({}),
  fetchIdentities: () => Promise.resolve([]),
  saveIdentityPair: () => Promise.resolve({ ok: true, total: 1 }),
}))
vi.mock('@/components/JobRunner', () => ({ default: () => null }))

beforeEach(() => {
  verifiedDomains.mockReset(); tenantInventory.mockReset(); allDomains.mockReset()
  verifiedDomains.mockResolvedValue({ domains: [
    { side: 'source', domain: 'src.example', adminEmail: 'admin@src.example',
      status: 'verified', live: 17, total: 17 },
    { side: 'target', domain: 'tgt.example', adminEmail: 'admin@tgt.example',
      status: 'pending', live: 3, total: 17 },
  ] })
  tenantInventory.mockResolvedValue({
    side: 'source', domain: 'src.example', accounts: 200, users: new Array(200),
    totals: { emails: 53211, threads: 0, driveBytes: 14e9, covered: 200 },
    truncated: false, error: '', deep: false, deepSampled: 0,
    licenseCounts: { 'Business Starter': 200 }, licenseError: '',
  })
  allDomains.mockResolvedValue({ superadmin: true, domains: [
    { accountId: 68, accountEmail: 'admin@bitport.local', side: 'source',
      domain: 'target.saraf.com', adminEmail: 'info@target.saraf.com',
      hasKey: true, clientId: '118368' },
    { accountId: 66, accountEmail: 'auto@bitport.local', side: 'target',
      domain: 'target.rohit.com.np', adminEmail: 'info@target.rohit.com.np',
      hasKey: true, clientId: '105869' },
    { accountId: 68, accountEmail: 'admin@bitport.local', side: 'target',
      domain: 'target.rohit.com.np', adminEmail: 'info@target.rohit.com.np',
      hasKey: false, clientId: '' },
  ] })
})

describe('the scoped-domain cards', () => {
  it('shows a card per configured domain', async () => {
    render(<Identities />)
    expect(await screen.findByTestId('domain-card-source')).toBeInTheDocument()
    expect(screen.getByTestId('domain-card-target')).toBeInTheDocument()
  })

  it('names the domain and its live scope count', async () => {
    render(<Identities />)
    const src = await screen.findByTestId('domain-card-source')
    expect(src).toHaveTextContent('src.example')
    expect(src).toHaveTextContent('17/17 live')
  })

  it('shows the status of each side', async () => {
    render(<Identities />)
    const tgt = await screen.findByTestId('domain-card-target')
    expect(tgt).toHaveTextContent('Propagating')
    expect(tgt).toHaveTextContent('3/17 live')
  })

  it('renders nothing when no domain is set up yet', async () => {
    verifiedDomains.mockResolvedValue({ domains: [] })
    render(<Identities />)
    await waitFor(() => expect(verifiedDomains).toHaveBeenCalled())
    expect(screen.queryByTestId('scoped-domains')).toBeNull()
  })

  it('does not crash the page when the domain check fails', async () => {
    verifiedDomains.mockRejectedValue(new Error('down'))
    render(<Identities />)
    // The identity map section still renders.
    expect(await screen.findByText('Load into identity_map')).toBeInTheDocument()
    expect(screen.queryByTestId('scoped-domains')).toBeNull()
  })
})


describe('clicking a domain shows its stats', () => {
  it('does not read the tenant until the card is opened', async () => {
    render(<Identities />)
    await screen.findByTestId('domain-card-source')
    expect(tenantInventory).not.toHaveBeenCalled()
    expect(screen.queryByTestId('domain-stats-source')).toBeNull()
  })

  it('fetches and shows users, data and licences on click', async () => {
    render(<Identities />)
    fireEvent.click(await screen.findByTestId('domain-card-open-source'))
    const stats = await screen.findByTestId('domain-stats-source')
    await waitFor(() => expect(tenantInventory).toHaveBeenCalledWith('source'))
    expect(stats).toHaveTextContent('200')            // users
    expect(stats).toHaveTextContent('14.0 GB')        // drive
    expect(stats).toHaveTextContent('53,211')         // email
    expect(stats).toHaveTextContent('Business Starter · 200')
  })

  it('is honest that licence counts are assigned, not free seats', async () => {
    render(<Identities />)
    fireEvent.click(await screen.findByTestId('domain-card-open-source'))
    const stats = await screen.findByTestId('domain-stats-source')
    expect(stats).toHaveTextContent(/free seats/i)
  })

  it('says so instead of reading a tenant that is not set up', async () => {
    verifiedDomains.mockResolvedValue({ domains: [
      { side: 'target', domain: 'tgt.example', adminEmail: 'a@tgt.example',
        status: 'not_set_up', live: 0, total: 0 },
    ] })
    render(<Identities />)
    fireEvent.click(await screen.findByTestId('domain-card-open-target'))
    expect(await screen.findByTestId('domain-stats-target'))
      .toHaveTextContent(/not set up/i)
    expect(tenantInventory).not.toHaveBeenCalled()
  })
})


describe('all configured domains (superadmin)', () => {
  it('lists every configured domain across accounts', async () => {
    render(<Identities />)
    await screen.findByTestId('all-domains')
    // Two accounts, three configured rows.
    expect(screen.getByTestId('config-68-source')).toHaveTextContent('target.saraf.com')
    expect(screen.getByTestId('config-66-target')).toHaveTextContent('target.rohit.com.np')
    expect(screen.getByTestId('config-68-target')).toBeInTheDocument()
  })

  it('shows which have a key on file and which do not', async () => {
    render(<Identities />)
    await screen.findByTestId('all-domains')
    expect(screen.getByTestId('config-68-source')).toHaveTextContent('Key on file')
    expect(screen.getByTestId('config-68-target')).toHaveTextContent('No key')
  })

  it('is hidden for a non-superadmin', async () => {
    allDomains.mockResolvedValue({ superadmin: false, domains: [
      { accountId: 68, accountEmail: '', side: 'source', domain: 'x.com',
        adminEmail: 'a@x.com', hasKey: true, clientId: '1' },
    ] })
    render(<Identities />)
    await screen.findByTestId('scoped-domains')
    expect(screen.queryByTestId('all-domains')).toBeNull()
  })

  it('does not break the page when the list cannot be read', async () => {
    allDomains.mockRejectedValue(new Error('down'))
    render(<Identities />)
    expect(await screen.findByText('Load into identity_map')).toBeInTheDocument()
    expect(screen.queryByTestId('all-domains')).toBeNull()
  })
})
