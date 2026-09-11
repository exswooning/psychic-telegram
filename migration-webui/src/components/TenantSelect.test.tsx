/**
 * "Account id" was a free-text number box in three places on the
 * Maintenance page -- next to "Wipe target accounts", where a mistyped
 * digit is somebody else's tenant rather than a failed job.
 *
 * Reported twice before this on other pages: "it gives me option to select
 * user ids not the verified domains", then "what is account id".
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import TenantSelect from './TenantSelect'

const me = vi.fn()
const admins = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => me(),
  fetchAdminAccounts: () => admins(),
}))

beforeEach(() => {
  me.mockReset(); admins.mockReset()
  me.mockResolvedValue({ id: 7, email: 'ops@x.test', is_superadmin: true })
  admins.mockResolvedValue([
    { id: 7, email: 'ops@x.test',
      source_domain: 'source.acme.test', target_domain: 'target.acme.test' },
    { id: 68, email: 'admin@bitport.local',
      source_domain: 'source.acme.test', target_domain: 'target.acme.test' },
    { id: 4, email: 'solo@z.test', source_domain: 'only.test' },
  ])
})

const Harness: React.FC = () => {
  const [v, setV] = React.useState('')
  return <TenantSelect value={v} onChange={setV} testid="pick" />
}

describe('picking a tenant', () => {
  it('lists domains, not row numbers', async () => {
    render(<Harness />)
    await waitFor(() => expect(admins).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByRole('combobox'))
    const opts = (await screen.findAllByRole('option')).map((o) => o.textContent || '')
    expect(opts.some((t) => t.includes('source.acme.test'))).toBe(true)
    expect(opts.some((t) => t.trim() === '7')).toBe(false)
  })

  it('keeps a blank option meaning "my own"', async () => {
    /* Every call site already treated "" that way, and the API treats an
       absent account as the caller's own. Replacing it with a sentinel
       number would push the same ambiguity one layer down. */
    render(<Harness />)
    fireEvent.mouseDown(screen.getByRole('combobox'))
    expect(await screen.findByRole('option', { name: 'My own tenant' }))
      .toBeInTheDocument()
  })

  it('reports the id as a string, which is what the callers send', async () => {
    render(<Harness />)
    await waitFor(() => expect(admins).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByRole('option', { name: /solo@z\.test|only\.test/ }))
    await waitFor(() =>
      expect(screen.getByTestId('pick')).toHaveValue('4'))
  })

  it('disambiguates tenants sharing a domain pair', async () => {
    render(<Harness />)
    await waitFor(() => expect(admins).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByRole('combobox'))
    const opts = (await screen.findAllByRole('option')).map((o) => o.textContent || '')
    expect(opts.filter((t) => t.includes('(')).length).toBe(2)
  })

  it('still works for a non-superadmin, who gets only their own', async () => {
    me.mockResolvedValue({ id: 7, email: 'ops@x.test', is_superadmin: false })
    render(<Harness />)
    await waitFor(() => expect(me).toHaveBeenCalled())
    expect(admins).not.toHaveBeenCalled()
    fireEvent.mouseDown(screen.getByRole('combobox'))
    expect(await screen.findByRole('option', { name: 'My own tenant' }))
      .toBeInTheDocument()
  })

  it('degrades to the blank option if the list cannot be read', async () => {
    admins.mockRejectedValue(new Error('nope'))
    render(<Harness />)
    await waitFor(() => expect(admins).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByRole('combobox'))
    expect(await screen.findByRole('option', { name: 'My own tenant' }))
      .toBeInTheDocument()
  })
})
