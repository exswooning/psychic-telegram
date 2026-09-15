/**
 * The seed path used to open on "sign in as a super admin" -- the same step
 * a first-time setup starts with -- so seeding a tenant set up weeks ago
 * asked for a Google password it had no use for. Seeding runs on the
 * service-account key already on file.
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom'

vi.mock('@/api/controlPlane', () => ({ fetchAllDomains: vi.fn() }))
import { fetchAllDomains } from '@/api/controlPlane'
import SeedDomainPicker from './SeedDomainPicker'

const all = fetchAllDomains as unknown as ReturnType<typeof vi.fn>

const dom = (over = {}) => ({
  accountId: 68, accountEmail: 'a@x', side: 'source' as const,
  domain: 'source.saraf.com', adminEmail: 'info@source.saraf.com',
  hasKey: true, clientId: '1', superseded: false, ...over,
})

beforeEach(() => { all.mockReset() })

describe('picking a tenant to seed', () => {
  it('shows a spinner until the domains are read', async () => {
    all.mockReturnValue(new Promise(() => {}))   // never resolves
    render(<SeedDomainPicker onPick={() => {}} onNew={() => {}} />)
    expect(screen.getByTestId('seed-domains-loading')).toBeInTheDocument()
  })

  it('offers each set-up domain as a card, and asks for no password', async () => {
    all.mockResolvedValue({ superadmin: true, domains: [
      dom(), dom({ domain: 'target.saraf.com', side: 'target',
                   adminEmail: 'info@target.saraf.com' }),
    ] })
    render(<SeedDomainPicker onPick={() => {}} onNew={() => {}} />)
    expect(await screen.findByTestId('seed-domain-source.saraf.com')).toBeInTheDocument()
    expect(screen.getByTestId('seed-domain-target.saraf.com')).toBeInTheDocument()
    expect(document.querySelector('input[type="password"]')).toBeNull()
  })

  it('hands back the domain and its admin when one is clicked', async () => {
    all.mockResolvedValue({ superadmin: true, domains: [dom()] })
    const onPick = vi.fn()
    render(<SeedDomainPicker onPick={onPick} onNew={() => {}} />)
    fireEvent.click(await screen.findByTestId('seed-domain-source.saraf.com'))
    expect(onPick).toHaveBeenCalledWith('source.saraf.com', 'info@source.saraf.com')
  })

  it('never offers a domain with no key -- the key is what seeds', async () => {
    all.mockResolvedValue({ superadmin: true, domains: [
      dom(), dom({ domain: 'nokey.com', hasKey: false }),
    ] })
    render(<SeedDomainPicker onPick={() => {}} onNew={() => {}} />)
    await screen.findByTestId('seed-domain-source.saraf.com')
    expect(screen.queryByTestId('seed-domain-nokey.com')).toBeNull()
  })

  it('shows one card per domain, not one per configured row', async () => {
    /* The same tenant is often set up under several accounts and in both
       slots, which rendered as identical cards that all did the same thing. */
    all.mockResolvedValue({ superadmin: true, domains: [
      dom(), dom({ accountId: 7 }), dom({ accountId: 66, side: 'target' }),
    ] })
    render(<SeedDomainPicker onPick={() => {}} onNew={() => {}} />)
    await screen.findByTestId('seed-domain-source.saraf.com')
    expect(screen.getAllByTestId('seed-domain-source.saraf.com')).toHaveLength(1)
  })

  it('says so when nothing on the box can be seeded', async () => {
    all.mockResolvedValue({ superadmin: true, domains: [] })
    render(<SeedDomainPicker onPick={() => {}} onNew={() => {}} />)
    expect(await screen.findByTestId('seed-no-domains')).toBeInTheDocument()
  })

  it('still offers setting up a new domain, which does need the sign-in', async () => {
    all.mockResolvedValue({ superadmin: true, domains: [dom()] })
    const onNew = vi.fn()
    render(<SeedDomainPicker onPick={() => {}} onNew={onNew} />)
    fireEvent.click(await screen.findByTestId('seed-new-domain'))
    expect(onNew).toHaveBeenCalled()
  })

  it('surfaces a read failure instead of rendering an empty page', async () => {
    all.mockRejectedValue(new Error('control plane is down'))
    render(<SeedDomainPicker onPick={() => {}} onNew={() => {}} />)
    await waitFor(() => expect(screen.getByText(/control plane is down/)).toBeInTheDocument())
  })
})
