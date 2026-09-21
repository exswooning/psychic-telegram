/**
 * A seed can run on a fleet node instead of this server.
 *
 * node_directives meant exactly one thing -- "migrate this tenant" -- so a
 * helper machine sitting idle while the coordinator ran hot could not be
 * given seed work instead. This is the UI half of fixing that: a "Run on"
 * selector beside the seed form, defaulting to "This server" (unchanged
 * behaviour), that writes a directive instead of starting a local process
 * when a node is chosen.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const runSeed = vi.fn().mockResolvedValue({ ok: true })
vi.mock('@/api/client', () => ({
  fetchStatus: () => Promise.resolve({ steps: [] }),
  checkStep: () => Promise.resolve({ ok: true }),
  fetchActions: () => Promise.resolve({}),
  fetchDwd: () => Promise.resolve({}),
  runSeed: (...a: unknown[]) => runSeed(...a),
  runResetTarget: () => Promise.resolve({ ok: true }),
}))

const fetchFleet = vi.fn()
const startSeedOnNode = vi.fn().mockResolvedValue({ run: true, kind: 'seed' })
const fetchDwdStatus = vi.fn().mockResolvedValue({})
// ONE factory for this module -- a second vi.mock('@/api/controlPlane', ...)
// elsewhere silently wins and this one never takes effect (see
// Wizard.flow.test.tsx's own comment on exactly this failure mode).
vi.mock('@/api/controlPlane', () => ({
  fetchDwdStatus: (...a: unknown[]) => fetchDwdStatus(...a),
  fetchFleet: () => fetchFleet(),
  startSeedOnNode: (...a: unknown[]) => startSeedOnNode(...a),
  stopNodeSeed: () => Promise.resolve({ run: false }),
}))

vi.mock('@/components/JobRunner', () => ({ default: () => null }))
vi.mock('@/components/JobProgress', () => ({ default: () => <div data-testid="job-progress" /> }))
vi.mock('@/components/CloudSetup', () => ({ default: () => null }))
vi.mock('@/components/OAuthConnect', () => ({ default: () => null }))
vi.mock('@/components/DwdSetup', () => ({ default: () => null }))
vi.mock('@/components/QuickTenantSetup', () => ({ default: () => null }))
vi.mock('@/components/DomainSandboxToggle', () => ({ default: () => null }))
vi.mock('@/components/SeedTopUp', () => ({ default: () => null }))

const NODE = { node_id: 'DESKTOP-6T3O8A3', hostname: 'DESKTOP-6T3O8A3',
  location: null, code_commit: null, last_seen: '', cpu_pct: null,
  ram_pct: 27.6, disk_pct: 69.1, active_job: null, job_pid: null,
  transfer_mode: null, users_done: 0, users_running: 0, users_failed: 0,
  error_rate: 0 }

beforeEach(() => {
  runSeed.mockClear(); startSeedOnNode.mockClear()
  fetchFleet.mockResolvedValue([NODE])
})

// SeedWizard's own tab-switching UI is not this test's concern -- render
// straight past it by going through the Setup Wizard's own default path is
// more machinery than this needs; SeedStep is exercised directly instead.
import { SeedStepForTests as SeedStep } from './SeedWizard'

describe('choosing a machine to seed on', () => {
  it('defaults to this server', async () => {
    render(<SeedStep domain="source.example.com" />)
    await screen.findByTestId('seed-run-on')
    expect(screen.getByTestId('seed-run-on')).toHaveTextContent('This server')
  })

  it('lists the fleet', async () => {
    render(<SeedStep domain="source.example.com" />)
    await waitFor(() => expect(fetchFleet).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByTestId('seed-run-on'))
    expect(await screen.findByTestId('seed-run-on-DESKTOP-6T3O8A3')).toBeInTheDocument()
  })

  it('starting on "This server" still calls the local runSeed, unchanged', async () => {
    render(<SeedStep domain="source.example.com" />)
    fireEvent.change(screen.getByLabelText(/domain to confirm/i) ??
                     screen.getAllByRole('textbox')[0], { target: { value: 'source.example.com' } })
    fireEvent.click(screen.getByRole('button', { name: /start seeding/i }))
    await waitFor(() => expect(runSeed).toHaveBeenCalled())
    expect(startSeedOnNode).not.toHaveBeenCalled()
  })

  it('carries accountId when the domain came from the cross-account picker', async () => {
    /* Without it the request resolves against whoever is SIGNED IN --
       "set the source domain in step 2 first" against the WRONG tenant,
       for a superadmin acting on a domain a different account owns. */
    render(<SeedStep domain="source.example.com" accountId={68} />)
    fireEvent.change(screen.getByLabelText(/domain to confirm/i) ??
                     screen.getAllByRole('textbox')[0], { target: { value: 'source.example.com' } })
    fireEvent.click(screen.getByRole('button', { name: /start seeding/i }))
    await waitFor(() => expect(runSeed).toHaveBeenCalled())
    const [, , , , opts] = runSeed.mock.calls[0]
    expect(opts).toMatchObject({ accountId: 68 })
  })

  it('starting on a chosen node calls startSeedOnNode, not the local run', async () => {
    render(<SeedStep domain="source.example.com" />)
    await waitFor(() => expect(fetchFleet).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByTestId('seed-run-on'))
    fireEvent.click(await screen.findByTestId('seed-run-on-DESKTOP-6T3O8A3'))
    fireEvent.click(screen.getByRole('button', { name: /start seeding on DESKTOP-6T3O8A3/i }))
    await waitFor(() => expect(startSeedOnNode).toHaveBeenCalled())
    expect(runSeed).not.toHaveBeenCalled()
    expect(await screen.findByTestId('seed-node-sent')).toHaveTextContent(/Nodes page/)
  })

  it('a refused directive surfaces the coordinator’s own error', async () => {
    startSeedOnNode.mockRejectedValueOnce(new Error('not a sandbox domain'))
    render(<SeedStep domain="source.example.com" />)
    await waitFor(() => expect(fetchFleet).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByTestId('seed-run-on'))
    fireEvent.click(await screen.findByTestId('seed-run-on-DESKTOP-6T3O8A3'))
    fireEvent.click(screen.getByRole('button', { name: /start seeding on DESKTOP-6T3O8A3/i }))
    expect(await screen.findByTestId('seed-node-error')).toHaveTextContent('not a sandbox domain')
  })

  it('does not poll for local job progress when a node is running it', async () => {
    render(<SeedStep domain="source.example.com" />)
    await waitFor(() => expect(fetchFleet).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByTestId('seed-run-on'))
    fireEvent.click(await screen.findByTestId('seed-run-on-DESKTOP-6T3O8A3'))
    expect(screen.queryByTestId('job-progress')).toBeNull()
  })
})
