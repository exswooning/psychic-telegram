import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import RemoveTenantSetup from './RemoveTenantSetup'

/* The most destructive control in the product. What matters is what it
   refuses to do. */

const tenants = [
  { side: 'source' as const, domain: 'src.example.com', project: 'p-1' },
  { side: 'target' as const, domain: 'tgt.example.com' },
]

const open = (onRemove = vi.fn()) => {
  render(<RemoveTenantSetup tenants={tenants} onRemove={onRemove} />)
  fireEvent.click(screen.getByTestId('remove-source'))
  return onRemove
}

describe('remove tenant setup', () => {
  it('shows nothing when no tenant is configured', () => {
    const { container } = render(
      <RemoveTenantSetup tenants={[{ side: 'source', domain: '' }]}
                         onRemove={vi.fn()} />)
    expect(container.textContent).toBe('')
  })

  it('refuses until the domain is typed exactly', () => {
    open()
    fireEvent.change(screen.getByTestId('admin-password'), { target: { value: 'pw' } })
    expect(screen.getByTestId('confirm-remove')).toBeDisabled()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'src.example.co' } })
    expect(screen.getByTestId('confirm-remove')).toBeDisabled()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'src.example.com' } })
    expect(screen.getByTestId('confirm-remove')).not.toBeDisabled()
  })

  it('will not accept the OTHER configured domain', () => {
    /* The mistake worth catching when two tenants are one click apart. */
    open()
    fireEvent.change(screen.getByTestId('admin-password'), { target: { value: 'pw' } })
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'tgt.example.com' } })
    expect(screen.getByTestId('confirm-remove')).toBeDisabled()
  })

  it('refuses without the admin password', () => {
    open()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'src.example.com' } })
    expect(screen.getByTestId('confirm-remove')).toBeDisabled()
  })

  it('passes the tenant and password through when confirmed', async () => {
    const onRemove = open()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'src.example.com' } })
    fireEvent.change(screen.getByTestId('admin-password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByTestId('confirm-remove'))
    await waitFor(() => expect(onRemove).toHaveBeenCalled())
    expect(onRemove.mock.calls[0][0].domain).toBe('src.example.com')
    expect(onRemove.mock.calls[0][1]).toBe('pw')
  })

  it('names what will be destroyed, including the project', () => {
    open()
    expect(screen.getByText(/p-1/)).toBeInTheDocument()
  })

  it('says the ledger is kept', () => {
    render(<RemoveTenantSetup tenants={tenants} onRemove={vi.fn()} />)
    expect(screen.getByText(/ledger is kept/i)).toBeInTheDocument()
  })
})
