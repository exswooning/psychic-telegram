/**
 * /api/run has always resolved a target through resolve_target_account --
 * an operator cleaning up somebody else's tenant is the normal case for
 * these tenant-wide steps -- but no caller ever sent one. Every button on
 * the Services page ran against whichever account the session happened to
 * resolve to, and nothing on screen named it. "Shared drives: migrate"
 * told you neither which tenant it read nor which it was about to write.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import TenantScopePicker from './TenantScopePicker'

const me = vi.fn()
const admins = vi.fn()
const cfg = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => me(),
  fetchAdminAccounts: () => admins(),
  fetchTenantConfigStatus: (...a: unknown[]) => cfg(...a),
}))

beforeEach(() => {
  // Reset call history, not just the resolved value: a superadmin test
  // leaving one call on the spy made "never asked for the account list"
  // fail in the test after it.
  me.mockReset(); admins.mockReset(); cfg.mockReset()
  me.mockResolvedValue({ id: 7, email: 'a@x.test', is_superadmin: false })
  admins.mockResolvedValue([])
  cfg.mockImplementation((side: string) => Promise.resolve({
    side, domain: side === 'source' ? 'src.example' : 'tgt.example' }))
})

const Harness: React.FC<{ onPick?: (id: number | undefined) => void }> =
  ({ onPick }) => {
    const [accountId, setAccountId] = React.useState<number | undefined>()
    return <TenantScopePicker accountId={accountId}
                              onAccountChange={(id) => {
                                setAccountId(id); onPick?.(id)
                              }} />
  }
import React from 'react'

describe('it names the tenant being acted on', () => {
  it('shows the source and the destination', async () => {
    render(<Harness />)
    await waitFor(() =>
      expect(screen.getByTestId('scope-source')).toHaveTextContent('src.example'))
    expect(screen.getByTestId('scope-target')).toHaveTextContent('tgt.example')
  })

  it('says so when nothing is configured, rather than showing blanks', async () => {
    cfg.mockResolvedValue({ domain: '' })
    render(<Harness />)
    expect(await screen.findByTestId('scope-unset')).toBeInTheDocument()
  })
})

describe('the chooser appears only where there is a choice', () => {
  it('is hidden for an account with only its own tenant', async () => {
    render(<Harness />)
    await waitFor(() => expect(me).toHaveBeenCalled())
    expect(screen.queryByTestId('scope-account')).toBeNull()
  })

  it('appears for a superadmin with several accounts', async () => {
    me.mockResolvedValue({ id: 1, email: 'ops@x.test', is_superadmin: true })
    admins.mockResolvedValue([
      { id: 1, email: 'ops@x.test' }, { id: 2, email: 'client@y.test' },
    ])
    render(<Harness />)
    expect(await screen.findByTestId('scope-account')).toBeInTheDocument()
  })

  it('does not ask a non-superadmin for the account list at all', async () => {
    render(<Harness />)
    await waitFor(() => expect(me).toHaveBeenCalled())
    expect(admins).not.toHaveBeenCalled()
  })
})

describe('changing the tenant', () => {
  it('reports the new account id upward', async () => {
    me.mockResolvedValue({ id: 1, email: 'ops@x.test', is_superadmin: true })
    admins.mockResolvedValue([
      { id: 1, email: 'ops@x.test' }, { id: 2, email: 'client@y.test' },
    ])
    const seen: (number | undefined)[] = []
    render(<Harness onPick={(id) => seen.push(id)} />)
    await screen.findByTestId('scope-account')
    // Open the menu and click the option. MUI renders a combobox over a
    // hidden input; firing change on that input does not reach the Select's
    // own handler, so it tested nothing.
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByRole('option', { name: 'client@y.test' }))
    await waitFor(() => expect(seen).toContain(2))
  })

  it('re-reads the domains for it', async () => {
    /* Showing the previous tenant's domains under a newly-picked account is
       the same lie as leaving stale inventory counts on screen. */
    me.mockResolvedValue({ id: 1, email: 'ops@x.test', is_superadmin: true })
    admins.mockResolvedValue([
      { id: 1, email: 'ops@x.test' }, { id: 2, email: 'client@y.test' },
    ])
    render(<Harness />)
    await screen.findByTestId('scope-account')
    const before = cfg.mock.calls.length
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByRole('option', { name: 'client@y.test' }))
    await waitFor(() => expect(cfg.mock.calls.length).toBeGreaterThan(before))
  })
})
