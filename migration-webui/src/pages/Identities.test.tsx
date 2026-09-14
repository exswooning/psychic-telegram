/**
 * The identity page maps source->target users, but that mapping is only
 * meaningful once the domains it maps between are actually set up. The
 * scoped-domain cards put that context on the page: which side, which admin,
 * and how many delegation scopes are live.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Identities from './Identities'

const verifiedDomains = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchVerifiedDomains: () => verifiedDomains(),
}))
vi.mock('@/api/client', () => ({
  fetchActions: () => Promise.resolve({}),
  fetchIdentities: () => Promise.resolve([]),
  saveIdentityPair: () => Promise.resolve({ ok: true, total: 1 }),
}))
vi.mock('@/components/JobRunner', () => ({ default: () => null }))

beforeEach(() => {
  verifiedDomains.mockResolvedValue({ domains: [
    { side: 'source', domain: 'src.example', adminEmail: 'admin@src.example',
      status: 'verified', live: 17, total: 17 },
    { side: 'target', domain: 'tgt.example', adminEmail: 'admin@tgt.example',
      status: 'pending', live: 3, total: 17 },
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
