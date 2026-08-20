import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import Nodes from './Nodes'

const claims = vi.fn()
const join = vi.fn()

vi.mock('@/api/controlPlane', () => ({
  fetchClaims: (...a: unknown[]) => claims(...a),
  fetchNodeJoin: (...a: unknown[]) => join(...a),
}))

/**
 * The page has one genuinely counterintuitive thing to communicate, and it
 * is the reason this file exists: an expired lease does NOT free the user
 * for another node. Resume is driven by the dead node's own local item
 * ledger, so a different machine restarting that user re-inserts everything
 * already delivered. A page that rendered "lease expired" as "available"
 * would invite exactly the silent duplication the claims table prevents.
 */
const claim = (over: Partial<Record<string, unknown>> = {}) => ({
  account_id: 7,
  source_user: 'alice@src.example.com',
  node_id: 'alpha',
  status: 'CLAIMED',
  services: 'gmail',
  claimed_at: new Date().toISOString(),
  renewed_at: new Date().toISOString(),
  lease_expires: new Date(Date.now() + 300000).toISOString(),
  forced_from: '',
  detail: '',
  live: true,
  stale: false,
  ...over,
})

const summary = (over = {}) => ({
  nodes: [{ node: 'alpha', claimed: 1, done: 2, failed: 0, stale: 0 }],
  total: 3, done: 2, failed: 0, stale: 0, ...over,
})

describe('Nodes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    join.mockResolvedValue({
      enabled: true, token: 'abcd••••••••wxyz', revealed: false,
      coordinatorUrl: 'https://cp.example.com', leaseSeconds: 300,
    })
  })

  it('shows nothing-yet rather than an empty table on a single-machine setup', async () => {
    /* Claiming only happens when BITPORT_COORDINATOR is set, so zero claims
       is the normal state, not a fault. */
    claims.mockResolvedValue({ claims: [], summary: summary({ total: 0, done: 0, nodes: [] }) })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('no-claims')).toBeTruthy())
    expect(screen.getByTestId('no-claims')).toHaveTextContent('BITPORT_COORDINATOR')
  })

  it('lists each claim with its node', async () => {
    claims.mockResolvedValue({ claims: [claim()], summary: summary() })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('claim-alice@src.example.com')).toBeTruthy())
    const row = screen.getByTestId('claim-alice@src.example.com')
    expect(row).toHaveTextContent('alpha')
    expect(row).toHaveTextContent('gmail')
    expect(row).toHaveTextContent('running')
  })

  it('warns that an expired lease is NOT free for another node', async () => {
    claims.mockResolvedValue({
      claims: [claim({ stale: true, live: false })],
      summary: summary({ stale: 1 }),
    })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('stale-warning')).toBeTruthy())
    const warn = screen.getByTestId('stale-warning')
    expect(warn).toHaveTextContent('not free for another node')
    expect(warn).toHaveTextContent(/re-deliver/)
  })

  it('does not warn when every lease is healthy', async () => {
    claims.mockResolvedValue({ claims: [claim()], summary: summary() })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('stat-total')).toBeTruthy())
    expect(screen.queryByTestId('stale-warning')).toBeNull()
  })

  it('shows a forced claim as forced, with where it came from', async () => {
    /* Forcing means accepting duplicates unless the target is cleaned
       first, so it is recorded rather than silent. */
    claims.mockResolvedValue({
      claims: [claim({ node_id: 'beta', forced_from: 'alpha' })],
      summary: summary(),
    })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('claim-alice@src.example.com')).toBeTruthy())
    expect(screen.getByTestId('claim-alice@src.example.com'))
      .toHaveTextContent('forced from alpha')
  })

  it('masks the node token until it is explicitly revealed', async () => {
    claims.mockResolvedValue({ claims: [], summary: summary({ total: 0, nodes: [] }) })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('join-command')).toBeTruthy())
    expect(screen.getByTestId('join-command')).toHaveTextContent('<node-token>')
    expect(screen.getByTestId('join-command')).not.toHaveTextContent('abcd')
  })

  it('puts the real token in the command only after Show token', async () => {
    claims.mockResolvedValue({ claims: [], summary: summary({ total: 0, nodes: [] }) })
    join.mockResolvedValueOnce({
      enabled: true, token: 'abcd••••••••wxyz', revealed: false,
      coordinatorUrl: 'https://cp.example.com', leaseSeconds: 300,
    }).mockResolvedValueOnce({
      enabled: true, token: 'real-secret-token', revealed: true,
      coordinatorUrl: 'https://cp.example.com', leaseSeconds: 300,
    })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('reveal-token')).toBeTruthy())
    fireEvent.click(screen.getByTestId('reveal-token'))
    await waitFor(() =>
      expect(screen.getByTestId('join-command')).toHaveTextContent('real-secret-token'))
  })

  it('says so when the control plane accepts no nodes at all', async () => {
    /* No token set means every claim is refused -- the intended default,
       not an oversight, and worth saying rather than showing a command that
       cannot work. */
    claims.mockResolvedValue({ claims: [], summary: summary({ total: 0, nodes: [] }) })
    join.mockResolvedValue({
      enabled: false, token: '', revealed: false, coordinatorUrl: '', leaseSeconds: 300,
    })
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('node-auth-off')).toBeTruthy())
    expect(screen.queryByTestId('join-command')).toBeNull()
  })

  it('explains rather than breaks when the join details are refused', async () => {
    /* Superadmin-only, so an ordinary account gets a 403 here -- the claims
       table above it must still render. */
    claims.mockResolvedValue({ claims: [claim()], summary: summary() })
    join.mockRejectedValue(new Error("'aryan' is not a superadmin"))
    render(<Nodes />)
    await waitFor(() => expect(screen.getByTestId('join-restricted')).toBeTruthy())
    expect(screen.getByTestId('claim-alice@src.example.com')).toBeTruthy()
  })
})
