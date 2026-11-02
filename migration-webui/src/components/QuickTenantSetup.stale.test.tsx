/** Live: mdharan.com's target card read "Setup done — provision accounts" over the
 *  users a previous setup created on target2.sarafgloabalexim.com. */
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import QuickTenantSetup from './QuickTenantSetup'

const none = vi.hoisted(() => () => Promise.resolve(null))
const ready = vi.hoisted(() => ({ value: false }))
const cfg = vi.hoisted(() => ({ value: null as unknown }))
const setupResult = vi.hoisted(() => ({ value: {
  side: 'target', ok: true, phases: [] as { name: string; detail: string }[],
  domain: 'target2.sarafgloabalexim.com', dryRun: false,
} }))
vi.mock('@/api/controlPlane', () => ({
  fetchFullSetupStatus: () => Promise.resolve({
    running: false,
    result: setupResult.value,
  }),
  fetchProvisionStatus: () => Promise.resolve({
    running: false, pid: null, created: 1, failed: 0, total: 1, tail: [],
    users: [{ email: 'fiona@target2.sarafgloabalexim.com', state: 'created' }],
  }),
  startFullSetup: none, startProvision: none,
  fetchReadiness: () => Promise.resolve({ side: 'target', purpose: 'migrate', domain: ready.value ? 't.example' : '', admin: '', ready: ready.value, checks: [] }),
  fetchTenantConfigStatus: () => Promise.resolve(cfg.value), uploadCredentials: none, fetchTenantInventory: none,
  startTenantScan: none, fetchTenantScan: none, buildIdentityMap: none,
  fetchIdentityMapStatus: none,
}))
vi.mock('@/api/client', () => ({ runSeed: vi.fn(), fetchSeedScopes: none }))
vi.mock('./ReasonCodeDialog', () => ({ default: () => null }))
vi.mock('./JobProgress', () => ({ default: () => null }))
vi.mock('./TenantInventoryPanel', () => ({ default: () => null }))
vi.mock('./ReprovisionPanel', () => ({ default: () => null }))
vi.mock('@/components/MfaBanner', () => ({ default: () => null }))
vi.mock('@/components/DomainSandboxToggle', () => ({ default: () => null }))

describe("another tenant's setup", () => {
  it('is not this one\'s "Setup done", nor its provisioned users', async () => {
    render(<MemoryRouter><QuickTenantSetup side="target" view="automated" showProvisionUsers
                                           initialDomain="mdharan.com" /></MemoryRouter>)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText(/Setup done/)).toBeNull()
    expect(screen.queryByText(/fiona@target2/)).toBeNull()
  })
  it('does not offer user provisioning after a successful dry-run preview', async () => {
    setupResult.value = { side: 'target', ok: true, domain: 'new.example', dryRun: true,
      phases: [{ name: 'domain-wide delegation (skipped: dry run)', detail: '' }] }
    render(<MemoryRouter><QuickTenantSetup side="target" view="automated" showProvisionUsers
                                           initialDomain="new.example" /></MemoryRouter>)
    expect(await screen.findByText(/Preview complete. No tenant connection/)).toBeInTheDocument()
    expect(screen.queryByText(/Setup done/)).toBeNull()
    expect(screen.queryByTestId('build-identity-map')).toBeNull()
  })
  it('offers mapping on a target already set up and Ready, with no setup run in this session', async () => {
    setupResult.value = null as unknown as typeof setupResult.value
    ready.value = true
    cfg.value = { hasKey: true, domain: 't.example', adminEmail: 'a@t.example', clientId: 'c1' }
    render(<MemoryRouter><QuickTenantSetup side="target" view="automated" showProvisionUsers
                                           initialDomain="t.example" /></MemoryRouter>)
    expect(await screen.findByTestId('map-users-only')).toBeInTheDocument()
    expect(screen.getByText(/Ready — map and provision accounts/)).toBeInTheDocument()
  })
})
