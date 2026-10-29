/** QuickTenantSetup asks about the viewed account and switches its actions off. */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import QuickTenantSetup from './QuickTenantSetup'

const none = vi.hoisted(() => () => Promise.resolve(null))
const calls = vi.hoisted(() => ({ cfg: vi.fn(), ready: vi.fn(), cap: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchFullSetupStatus: () => Promise.resolve({ running: false, result: null }),
  fetchProvisionStatus: none, startFullSetup: none, startProvision: none,
  fetchReadiness: (...a: unknown[]) => {
    calls.ready(...a)
    return Promise.resolve({ side: 'target', purpose: 'migrate', domain: 't.com', admin: '', ready: true, checks: [] })
  },
  fetchTenantConfigStatus: (...a: unknown[]) => {
    calls.cfg(...a)
    return Promise.resolve({ side: 'target', domain: 't.com', adminEmail: 'a@t.com', hasKey: true, clientId: '1', scopes: [] })
  },
  fetchAllDomains: () => Promise.resolve({ superadmin: true, domains: [] }),
  reuseKey: none,
  fetchCapacity: (...a: unknown[]) => {
    calls.cap(...a)
    return Promise.resolve({ side: 'target', users: 1, storage: { limit: 1, usage: 0 }, errors: {}, checkedAt: 'x' })
  },
  uploadCredentials: none, fetchTenantInventory: none, startTenantScan: none, fetchTenantScan: none,
  buildIdentityMap: none, fetchIdentityMapStatus: none,
}))
vi.mock('@/api/client', () => ({ runSeed: vi.fn(), fetchSeedScopes: none }))
vi.mock('./ReasonCodeDialog', () => ({ default: () => null }))
vi.mock('./JobProgress', () => ({ default: () => null }))
vi.mock('./TenantInventoryPanel', () => ({ default: () => null }))
vi.mock('./ReprovisionPanel', () => ({ default: () => null }))
vi.mock('@/components/MfaBanner', () => ({ default: () => null }))
vi.mock('@/components/DomainSandboxToggle', () => ({ default: () => null }))
vi.mock('./GovExactCopy', () => ({ default: () => null }))
vi.mock('./DeviceHandoff', () => ({ default: () => null }))

describe('viewing another account', () => {
  it('passes it to every read and says actions are off', async () => {
    render(<MemoryRouter><QuickTenantSetup side="target" view="automated" initialDomain="t.com" viewedAccount={9} /></MemoryRouter>)
    await waitFor(() => expect(calls.cap).toHaveBeenCalledWith('target', 9))
    expect(calls.cfg).toHaveBeenCalledWith('target', 9)
    expect(calls.ready).toHaveBeenCalledWith('target', 'migrate', 9)
    expect(screen.getByTestId('qts-readonly')).toBeInTheDocument()
    // Re-asking Google changes nothing, so it stays available on another account.
    const before = calls.ready.mock.calls.length
    const again = screen.getByTestId('qts-readonly-recheck')
    // Capacity and readiness resolve independently. Wait for the readiness
    // request's finally handler before testing the next user interaction.
    await waitFor(() => expect(again).not.toBeDisabled())
    fireEvent.click(again)
    await waitFor(() => expect(calls.ready.mock.calls.length).toBeGreaterThan(before))
  })
})
