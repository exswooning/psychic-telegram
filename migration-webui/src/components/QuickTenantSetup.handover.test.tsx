/**
 * The wizard already asked for the admin's password; the setup card asked again
 * through its own sign-in dialog. And a tenant picked as "already set up" offered
 * a sign-in it does not need.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import QuickTenantSetup from './QuickTenantSetup'

const none = vi.hoisted(() => () => Promise.resolve(null))
const started = vi.hoisted(() => vi.fn(() => Promise.resolve({ ok: true, detail: '' })))
const cfg = vi.hoisted(() => ({ value: null as unknown }))
const ready = vi.hoisted(() => ({ value: null as unknown }))
vi.mock('@/api/controlPlane', () => ({
  fetchFullSetupStatus: () => Promise.resolve({ running: false, result: null }),
  fetchReadiness: () => Promise.resolve(ready.value),
  fetchExactCopy: () => Promise.resolve({ government: false, domain: "", on: false }),
  fetchTenantConfigStatus: () => Promise.resolve(cfg.value),
  startFullSetup: started, startProvision: none, fetchProvisionStatus: none,
  uploadCredentials: none, fetchTenantInventory: none,
  startTenantScan: none, fetchTenantScan: none, buildIdentityMap: none,
  fetchIdentityMapStatus: none,
}))
vi.mock('@/api/client', () => ({ runSeed: vi.fn(), fetchSeedScopes: none }))
vi.mock('./ReasonCodeDialog', () => ({ default: () => null }))
vi.mock('./JobProgress', () => ({ default: () => null }))
vi.mock('./TenantInventoryPanel', () => ({ default: () => null }))
vi.mock('./ReprovisionPanel', () => ({ default: () => null }))
vi.mock('@/components/MfaBanner', () => ({ default: () => null }))
vi.mock('./DomainSandboxToggle', () => ({ default: () => null }))
vi.mock('@/components/DomainSandboxToggle', () => ({ default: () => null }))

beforeEach(() => { started.mockClear(); cfg.value = null; ready.value = null })

describe('a password the wizard already has', () => {
  it('starts the real setup in one click, without the sign-in dialog', async () => {
    render(<MemoryRouter><QuickTenantSetup side="source" view="automated"
      initialDomain="acme.com" initialEmail="info@acme.com" initialPassword="pw12345678" /></MemoryRouter>)
    const go = await screen.findByTestId('qts-source-go')
    expect(screen.getByText(/using the password from the previous step/)).toBeInTheDocument()
    fireEvent.click(go)
    await waitFor(() => expect(started).toHaveBeenCalled())
    const call = started.mock.calls[0] as unknown[]
    expect(call.slice(1, 5)).toEqual(['source', 'acme.com', 'info@acme.com', 'pw12345678'])
    expect((call[5] as { dryRun: boolean }).dryRun).toBe(false)
  })

  it('still asks when nothing was handed over', async () => {
    render(<MemoryRouter><QuickTenantSetup side="source" view="automated"
      initialDomain="acme.com" initialEmail="info@acme.com" /></MemoryRouter>)
    await screen.findByText(/Continue as info@acme.com/)
    expect(screen.queryByTestId('qts-source-go')).toBeNull()
  })
})

describe('a tenant already set up in this slot', () => {
  it('says so instead of offering a sign-in', async () => {
    cfg.value = { side: 'target', domain: 'newco.com', adminEmail: 'info@newco.com',
                  hasKey: true, clientId: '1136', scopes: [] }
    ready.value = { side: 'target', purpose: 'migrate', domain: 'newco.com', admin: 'info@newco.com',
                    ready: true, checks: [{ name: 'delegation', ok: true, detail: 'all' }] }
    render(<MemoryRouter><QuickTenantSetup side="target" view="automated"
      initialDomain="newco.com" initialEmail="info@newco.com" /></MemoryRouter>)
    expect(await screen.findByTestId('qts-target-ready')).toHaveTextContent('already set up')
    expect(screen.queryByText(/Continue as/)).toBeNull()
  })

  it('does not claim it while readiness says Not ready (a key on file is not enough)', async () => {
    cfg.value = { side: 'target', domain: 'newco.com', adminEmail: 'info@newco.com',
                  hasKey: true, clientId: '1136', scopes: [] }
    ready.value = { side: 'target', purpose: 'migrate', domain: 'newco.com', admin: 'info@newco.com',
                    ready: false, checks: [{ name: 'delegation', ok: false, detail: 'refused' }] }
    render(<MemoryRouter><QuickTenantSetup side="target" view="automated"
      initialDomain="newco.com" initialEmail="info@newco.com" /></MemoryRouter>)
    await screen.findByText(/Continue as info@newco.com/)
    expect(screen.queryByTestId('qts-target-ready')).toBeNull()
  })
})

describe('readiness, asked of Google rather than taken from setup', () => {
  const setUp = () => {
    cfg.value = { side: 'source', domain: 'acme.com', adminEmail: 'info@acme.com',
                  hasKey: true, clientId: '115', scopes: [] }
  }

  it('shows Not ready with the failing check, for a key the tenant refuses', async () => {
    setUp()
    ready.value = { side: 'source', purpose: 'migrate', domain: 'acme.com', admin: 'info@acme.com',
                    ready: false, checks: [
                      { name: 'configured', ok: true, detail: 'acme.com' },
                      { name: 'delegation', ok: false, detail: '18 of 18 scopes do not mint' }] }
    render(<MemoryRouter><QuickTenantSetup side="source" view="automated"
      initialDomain="acme.com" initialEmail="info@acme.com" /></MemoryRouter>)
    const panel = await screen.findByTestId('qts-source-readiness')
    await waitFor(() => expect(panel).toHaveTextContent('Not ready'))
    expect(panel).toHaveTextContent('18 of 18 scopes do not mint')
    expect(screen.queryByTestId('go-to-jobs')).toBeNull()
  })

  it('shows Ready when every check passes', async () => {
    setUp()
    ready.value = { side: 'source', purpose: 'migrate', domain: 'acme.com', admin: 'info@acme.com',
                    ready: true, checks: [{ name: 'delegation', ok: true, detail: 'all 18' }] }
    render(<MemoryRouter><QuickTenantSetup side="source" view="automated"
      initialDomain="acme.com" initialEmail="info@acme.com" /></MemoryRouter>)
    const panel = await screen.findByTestId('qts-source-readiness')
    await waitFor(() => expect(panel).toHaveTextContent(/\bReady\b/))
  })
})
