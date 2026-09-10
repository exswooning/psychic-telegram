/**
 * /api/run has always resolved a target through resolve_target_account --
 * an operator cleaning up somebody else's tenant is the normal case for
 * these tenant-wide steps -- but no caller ever sent one. Every button on
 * the Services page ran against whichever account the session happened to
 * resolve to, and nothing on screen named it. "Shared drives: migrate"
 * told you neither which tenant it read nor which it was about to write.
 *
 * The first chooser then named the wrong thing: it listed the Bitport
 * LOGINS the tenants belong to -- ops@x.test, client@y.test -- when
 * nothing on the page acts on an account. Reported live as "it gives me
 * option to select user ids not the verified domains".
 */
import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import TenantScopePicker from './TenantScopePicker'

const me = vi.fn()
const admins = vi.fn()
const verified = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => me(),
  fetchAdminAccounts: () => admins(),
  fetchVerifiedDomains: (...a: unknown[]) => verified(...a),
}))

/** A superadmin looking at two tenants -- the only case with a chooser. */
const twoTenants = () => {
  me.mockResolvedValue({ id: 1, email: 'ops@x.test', is_superadmin: true })
  admins.mockResolvedValue([
    { id: 1, email: 'ops@x.test',
      source_domain: 'src.example', target_domain: 'tgt.example' },
    { id: 2, email: 'client@y.test',
      source_domain: 'old.acme.test', target_domain: 'new.acme.test' },
  ])
}

beforeEach(() => {
  // Reset call history, not just the resolved value: a superadmin test
  // leaving one call on the spy made "never asked for the account list"
  // fail in the test after it.
  me.mockReset(); admins.mockReset(); verified.mockReset()
  me.mockResolvedValue({ id: 7, email: 'a@x.test', is_superadmin: false })
  admins.mockResolvedValue([])
  verified.mockResolvedValue({ domains: [
    { side: 'source', domain: 'src.example', status: 'verified', live: 9, total: 9 },
    { side: 'target', domain: 'tgt.example', status: 'pending', live: 4, total: 19 },
  ] })
})

const Harness: React.FC<{ onPick?: (id: number | undefined) => void }> =
  ({ onPick }) => {
    const [accountId, setAccountId] = React.useState<number | undefined>()
    return <TenantScopePicker accountId={accountId}
                              onAccountChange={(id) => {
                                setAccountId(id); onPick?.(id)
                              }} />
  }

describe('it names the tenant being acted on', () => {
  it('shows the source and the destination', async () => {
    render(<Harness />)
    await waitFor(() =>
      expect(screen.getByTestId('scope-source')).toHaveTextContent('src.example'))
    expect(screen.getByTestId('scope-target')).toHaveTextContent('tgt.example')
  })

  it('says a domain is not verified rather than implying it is', async () => {
    /* An unverified domain is one every button here will fail against.
       Saying so costs a word; finding out costs a run. */
    render(<Harness />)
    await waitFor(() =>
      expect(screen.getByTestId('scope-target')).toHaveTextContent('pending'))
    expect(screen.getByTestId('scope-source')).not.toHaveTextContent('verified')
  })

  it('says so when nothing is configured, rather than showing blanks', async () => {
    verified.mockResolvedValue({ domains: [] })
    render(<Harness />)
    expect(await screen.findByTestId('scope-unset')).toBeInTheDocument()
  })
})

describe('the chooser lists domains, not logins', () => {
  it('labels each option with the tenant domain', async () => {
    twoTenants()
    render(<Harness />)
    await screen.findByTestId('scope-account')
    fireEvent.mouseDown(screen.getByRole('combobox'))
    const options = (await screen.findAllByRole('option'))
      .map((o) => o.textContent || '')
    expect(options.some((t) => t.includes('old.acme.test'))).toBe(true)
    // The login is the thing being complained about: it must not be how a
    // configured tenant is named.
    expect(options.some((t) => t.includes('client@y.test'))).toBe(false)
  })

  it('tells apart two accounts set up against the same domain', async () => {
    /* Live, three accounts point at source.rohitrokaya.com.np. Four
       identical rows is the same "which one is which" problem the logins
       had, so the login comes back as a tiebreak -- on those rows only. */
    me.mockResolvedValue({ id: 1, email: 'ops@x.test', is_superadmin: true })
    admins.mockResolvedValue([
      { id: 1, email: 'ops@x.test',
        source_domain: 'src.example', target_domain: 'tgt.example' },
      { id: 2, email: 'client@y.test',
        source_domain: 'src.example', target_domain: 'tgt.example' },
      { id: 3, email: 'solo@z.test',
        source_domain: 'only.example', target_domain: 'other.example' },
    ])
    render(<Harness />)
    await screen.findByTestId('scope-account')
    fireEvent.mouseDown(screen.getByRole('combobox'))
    const options = (await screen.findAllByRole('option'))
      .map((o) => o.textContent || '')
    expect(options).toContain('src.example \u2192 tgt.example (ops@x.test)')
    expect(options).toContain('src.example \u2192 tgt.example (client@y.test)')
    // The unambiguous row stays clean -- a tiebreak nobody needs is noise.
    expect(options).toContain('only.example \u2192 other.example')
  })

  it('falls back to the login for an account whose wizard has not run', async () => {
    /* An empty row would be unpickable, which is worse than a login. */
    me.mockResolvedValue({ id: 1, email: 'ops@x.test', is_superadmin: true })
    admins.mockResolvedValue([
      { id: 1, email: 'ops@x.test', source_domain: 'src.example' },
      { id: 2, email: 'fresh@z.test', source_domain: null, target_domain: null },
    ])
    render(<Harness />)
    await screen.findByTestId('scope-account')
    fireEvent.mouseDown(screen.getByRole('combobox'))
    expect(await screen.findByRole('option', { name: /fresh@z\.test/ }))
      .toBeInTheDocument()
  })
})

describe('the chooser appears only where there is a choice', () => {
  it('is hidden for an account with only its own tenant', async () => {
    render(<Harness />)
    await waitFor(() => expect(me).toHaveBeenCalled())
    expect(screen.queryByTestId('scope-account')).toBeNull()
  })

  it('appears for a superadmin with several accounts', async () => {
    twoTenants()
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
    twoTenants()
    const seen: (number | undefined)[] = []
    render(<Harness onPick={(id) => seen.push(id)} />)
    await screen.findByTestId('scope-account')
    // Open the menu and click the option. MUI renders a combobox over a
    // hidden input; firing change on that input does not reach the Select's
    // own handler, so it tested nothing.
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByRole('option', { name: /old\.acme\.test/ }))
    await waitFor(() => expect(seen).toContain(2))
  })

  it('asks about THAT account, not the caller', async () => {
    /* The endpoint answered about whoever was logged in until it took an
       account_id, so picking another tenant showed your own domains back
       with nothing saying so -- the two things a chooser exists to tell
       apart, rendered identically. */
    twoTenants()
    render(<Harness />)
    await screen.findByTestId('scope-account')
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByRole('option', { name: /old\.acme\.test/ }))
    await waitFor(() => expect(verified).toHaveBeenCalledWith(2))
  })
})
