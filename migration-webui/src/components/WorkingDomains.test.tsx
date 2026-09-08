import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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

  it('offers both actions per domain', () => {
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
})
