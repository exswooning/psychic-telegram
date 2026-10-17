/**
 * A setup run killed by a server restart is written as
 * {running:false, interrupted:true, phases:[], error:"...start it again"}.
 * The panel drew only `phases`, so the operator saw a red "failed" over an
 * empty box with no hint that nothing was wrong with the tenant.
 */
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import QuickTenantSetup from './QuickTenantSetup'

const none = vi.hoisted(() => () => Promise.resolve(null))
const was = vi.hoisted(() => ({ domain: 'acme.com' as string | null }))
vi.mock('@/api/controlPlane', () => ({
  fetchFullSetupStatus: () => Promise.resolve({
    running: false,
    result: { side: 'source', ok: false, phases: [], interrupted: true, domain: was.domain,
              error: 'this setup run was interrupted when the server restarted' },
  }),
  startFullSetup: none, startProvision: none, fetchProvisionStatus: none,
  fetchTenantConfigStatus: none, uploadCredentials: none, fetchTenantInventory: none,
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

describe('an interrupted setup run', () => {
  it('says so, in words, instead of a bare "failed"', async () => {
    was.domain = 'acme.com'
    render(<MemoryRouter><QuickTenantSetup side="source" view="automated" initialDomain="acme.com" /></MemoryRouter>)
    expect(await screen.findByText(/interrupted when the server restarted/)).toBeInTheDocument()
    expect(screen.getByText('interrupted')).toBeInTheDocument()
    expect(screen.queryByText('failed')).toBeNull()
  })

  /* Live: a fresh pair opened under two "interrupted" cards from runs a month
     earlier for another tenant, which named no domain at all. */
  it.each([['another tenant', 'old.com'], ['no recorded domain', null]])(
    'is not shown beside a different setup (%s)', async (_why, d) => {
      was.domain = d
      render(<MemoryRouter><QuickTenantSetup side="source" view="automated" initialDomain="acme.com" /></MemoryRouter>)
      await new Promise((r) => setTimeout(r, 50))
      expect(screen.queryByText(/interrupted when the server restarted/)).toBeNull()
    })
})
