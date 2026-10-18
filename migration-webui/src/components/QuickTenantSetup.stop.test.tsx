/** Live: a sign-in stuck on Google asking the server's browser for a phone number,
 *  with nothing on the card to end it and start again. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import QuickTenantSetup from './QuickTenantSetup'

const none = vi.hoisted(() => () => Promise.resolve(null))
const stop = vi.hoisted(() => vi.fn(() => Promise.resolve({ ok: true })))
vi.mock('@/api/controlPlane', () => ({
  fetchFullSetupStatus: () => Promise.resolve({ running: true, pid: 4321, result: null, progressPct: 5 }),
  stopFullSetup: stop,
  startFullSetup: none, startProvision: none, fetchProvisionStatus: none,
  fetchReadiness: () => Promise.resolve({ side: 'source', purpose: 'migrate', domain: '', admin: '', ready: false, checks: [] }),
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
vi.mock('@/components/DomainSandboxToggle', () => ({ default: () => null }))

describe('a running setup', () => {
  it('can be stopped from its card', async () => {
    render(<MemoryRouter><QuickTenantSetup side="source" view="automated"
                                           initialDomain="dharan.gov.np" /></MemoryRouter>)
    fireEvent.click(await screen.findByTestId('qts-source-stop'))
    await waitFor(() => expect(stop).toHaveBeenCalledWith('source'))
  })
})
