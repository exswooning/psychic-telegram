/** A superadmin can give a keyless side a key another account already holds; a keyed side
 *  shows how many accounts and how much storage the tenant has. */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import QuickTenantSetup from './QuickTenantSetup'

const none = vi.hoisted(() => () => Promise.resolve(null))
const cfg = vi.hoisted(() => ({ value: { side: 'target', domain: '', adminEmail: '', hasKey: false, clientId: '', scopes: [] } as unknown }))
const reuseKey = vi.hoisted(() => vi.fn())
const capacity = vi.hoisted(() => ({ value: null as unknown }))
vi.mock('@/api/controlPlane', () => ({
  fetchFullSetupStatus: () => Promise.resolve({ running: false, result: null }),
  fetchProvisionStatus: none, startFullSetup: none, startProvision: none,
  fetchReadiness: () => Promise.resolve({ side: 'target', purpose: 'migrate', domain: 't.com', admin: '', ready: true, checks: [] }),
  fetchTenantConfigStatus: () => Promise.resolve(cfg.value),
  fetchAllDomains: () => Promise.resolve({ superadmin: true, domains: [
    { accountId: 7, side: 'target', domain: 'old.com', hasKey: true, superseded: false },
    { accountId: 8, side: 'source', domain: 'src.com', hasKey: true, superseded: false }] }),
  reuseKey: (...a: unknown[]) => reuseKey(...a),
  fetchCapacity: () => Promise.resolve(capacity.value),
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

const view = () => render(<MemoryRouter><QuickTenantSetup side="target" view="automated" initialDomain="t.com" /></MemoryRouter>)

describe('reusing a key on this server', () => {
  it('lists only this side\'s keys and copies the one picked', async () => {
    reuseKey.mockResolvedValue({ ok: true, detail: 'copied' })
    view()
    fireEvent.click(await screen.findByText('old.com (account 7)'))
    expect(screen.queryByText(/src.com/)).toBeNull()
    await waitFor(() => expect(reuseKey).toHaveBeenCalledWith(expect.any(String), 'target', 7))
  })
})

describe('tenant capacity', () => {
  it('says accounts and storage used of the limit, and a limit of 0 plainly', async () => {
    cfg.value = { side: 'target', domain: 't.com', adminEmail: 'a@t.com', hasKey: true, clientId: '1', scopes: [] }
    capacity.value = { side: 'target', users: 12, storage: { limit: 30e9, usage: 5e9 }, errors: {}, checkedAt: 'x' }
    const { unmount } = view()
    expect(await screen.findByTestId('qts-target-capacity')).toHaveTextContent('12 accounts · 5.0 GB used of 30.0 GB')
    unmount()
    capacity.value = { side: 'target', users: 12, storage: { limit: 0, usage: 0 }, errors: {}, checkedAt: 'x' }
    view()
    expect(await screen.findByText(/storage limit 0 — this tenant cannot store files/)).toBeInTheDocument()
  })
})
