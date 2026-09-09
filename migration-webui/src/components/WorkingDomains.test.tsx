import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
// The hook reaches @/api/controlPlane, which reads localStorage at module
// load. Mocked at the hook rather than the api module so a test can also
// say what is running.
const runningJobs = vi.hoisted(() => ({ current: [] as any[] }))
vi.mock('@/hooks/useRunningJobs', () => ({
  useRunningJobs: () => ({ jobs: runningJobs.current, loading: false,
                           refresh: vi.fn() }),
}))

import WorkingDomains from './WorkingDomains'

/* Two very different actions behind one card, which is the point of showing
   them together -- and the reason each is gated on the domain rather than a
   generic word: the mistake worth catching is acting on the wrong one of two
   tenants that sit a click apart. */

const tenants = [
  { side: 'source' as const, domain: 'src.example.com', project: 'p-1',
    adminEmail: 'admin@src.example.com', clientId: '123' },
  { side: 'target' as const, domain: 'tgt.example.com' },
]

const open = (which: 'wipe' | 'remove', onAct = vi.fn()) => {
  render(<WorkingDomains tenants={tenants} onAct={onAct} />)
  fireEvent.click(screen.getByTestId(`${which}-source`))
  return onAct
}

describe('working domains', () => {
  it('lists the configured domains with what they are set up as', () => {
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    expect(screen.getByText('src.example.com')).toBeInTheDocument()
    expect(screen.getByText('tgt.example.com')).toBeInTheDocument()
    expect(screen.getByText(/admin@src.example.com/)).toBeInTheDocument()
    expect(screen.getByText(/client 123/)).toBeInTheDocument()
  })

  it('says so when nothing is set up rather than showing an empty card', () => {
    render(<WorkingDomains tenants={[{ side: 'source', domain: '' }]}
                           onAct={vi.fn()} />)
    expect(screen.getByText(/No tenant is set up yet/)).toBeInTheDocument()
  })

  it('offers all three actions per domain', () => {
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    expect(screen.getByTestId('wipe-source')).toBeInTheDocument()
    expect(screen.getByTestId('remove-source')).toBeInTheDocument()
    expect(screen.getByTestId('wipe-target')).toBeInTheDocument()
  })

  it('tells the wipe apart from the removal in words, not just colour', () => {
    open('wipe')
    expect(screen.getByText(/stays ready to seed or migrate again/))
      .toBeInTheDocument()
  })

  it('says a removal needs a fresh sign-in and grant afterwards', () => {
    open('remove')
    expect(screen.getByText(/fresh sign-in and a fresh grant/)).toBeInTheDocument()
  })

  it('does not ask for a password to wipe', () => {
    /* A wipe uses the service account already on file; demanding a
       credential nothing will use trains people to type it anywhere. */
    open('wipe')
    expect(screen.queryByTestId('admin-password')).toBeNull()
  })

  it('does ask for one to remove, because that signs in to Google', () => {
    open('remove')
    expect(screen.getByTestId('admin-password')).toBeInTheDocument()
  })

  it('refuses until the exact domain is typed', () => {
    open('wipe')
    expect(screen.getByTestId('confirm-act')).toBeDisabled()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'src.example.co' } })
    expect(screen.getByTestId('confirm-act')).toBeDisabled()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'src.example.com' } })
    expect(screen.getByTestId('confirm-act')).not.toBeDisabled()
  })

  it('will not accept the other configured domain', () => {
    open('wipe')
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'tgt.example.com' } })
    expect(screen.getByTestId('confirm-act')).toBeDisabled()
  })

  it('passes the mode through, so a wipe never becomes a removal', async () => {
    const onAct = open('wipe')
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'src.example.com' } })
    fireEvent.click(screen.getByTestId('confirm-act'))
    await waitFor(() => expect(onAct).toHaveBeenCalled())
    expect(onAct.mock.calls[0][1]).toBe('wipe')
  })

  it('says the ledger is never touched', () => {
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    expect(screen.getByText(/ledger is never touched/i)).toBeInTheDocument()
  })

  it('offers deleting the accounts, not just their data', () => {
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    expect(screen.getByTestId('delete-users-source')).toBeInTheDocument()
    expect(screen.getByTestId('delete-users-target')).toBeInTheDocument()
  })

  it('says plainly that it takes the accounts themselves', async () => {
    /* The distinction that matters: wipe empties a tenant and leaves it
       usable, this removes the users it was emptying. */
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    fireEvent.click(screen.getByTestId('delete-users-source'))
    expect(await screen.findByText(/every migrated account/i)).toBeInTheDocument()
    expect(screen.getByText(/reserved for 20 days/i)).toBeInTheDocument()
  })

  it('still demands the domain typed back before deleting accounts', () => {
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    fireEvent.click(screen.getByTestId('delete-users-source'))
    const confirm = screen.getByRole('button', { name: /delete users/i })
    expect(confirm).toBeDisabled()
  })

  it('offers the console repair, and does not dress it as destruction', () => {
    /* It re-pastes a grant and configures a Chat app. Adds nothing and
       deletes nothing -- the only action on this card that does not. */
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    fireEvent.click(screen.getByTestId('repair-source'))
    expect(screen.getByText(/Adds nothing and deletes nothing/i)).toBeInTheDocument()
  })

  it('says what the repair is for, since both steps have no API', async () => {
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    fireEvent.click(screen.getByTestId('repair-source'))
    expect(await screen.findByText(/scope added since/i)).toBeInTheDocument()
    expect(screen.getByText(/Chat app/i)).toBeInTheDocument()
  })

  it('asks for the admin password, because both steps sign in', () => {
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    fireEvent.click(screen.getByTestId('repair-source'))
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument()
  })

  it('shows a job running against this tenant, with a bar', () => {
    /* Every action here starts a job somewhere else and used to say nothing
       more about it -- a repair ran for twelve seconds while this card
       showed four idle buttons, which reads as "click it again", on buttons
       that wipe tenants. */
    runningJobs.current = [{
      key: 'k', kind: 'setup', label: 'repair console setup',
      domain: 'src.example.com', detail: '12.6s · re-granting delegation', pct: null,
    }]
    const { container } = render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    expect(screen.getByTestId('running-source')).toBeInTheDocument()
    expect(screen.getByText('repair console setup')).toBeInTheDocument()
    expect(container.querySelector('.MuiLinearProgress-root')).toBeTruthy()
    runningJobs.current = []
  })

  it('leaves the other tenant alone', () => {
    runningJobs.current = [{
      key: 'k', kind: 'reset', label: 'wipe tenant data',
      domain: 'src.example.com', detail: '3m', pct: 40,
    }]
    render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    expect(screen.getByTestId('running-source')).toBeInTheDocument()
    expect(screen.queryByTestId('running-target')).not.toBeInTheDocument()
    runningJobs.current = []
  })

  it('shows no bar at all when nothing is running', () => {
    runningJobs.current = []
    const { container } = render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    expect(screen.queryByTestId('running-source')).not.toBeInTheDocument()
    expect(container.querySelector('.MuiLinearProgress-root')).toBeNull()
  })

  it('uses a real percentage when the job reports one', () => {
    /* A full bar on a job with no percentage is a worse lie than no bar. */
    runningJobs.current = [{
      key: 'k', kind: 'reset', label: 'wipe tenant data',
      domain: 'src.example.com', detail: '40%', pct: 40,
    }]
    const { container } = render(<WorkingDomains tenants={tenants} onAct={vi.fn()} />)
    const bar = container.querySelector('.MuiLinearProgress-determinate')
    expect(bar).toBeTruthy()
    expect(bar?.getAttribute('aria-valuenow')).toBe('40')
    runningJobs.current = []
  })
})
