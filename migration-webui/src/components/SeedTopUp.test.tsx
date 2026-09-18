/**
 * seed_sandbox.py has no "already seeded" check of its own -- every run is
 * additive, on top of whoever already exists. This is the UI that makes
 * that safe by construction: no create-users, no reset, no all-users/
 * create-until-full. What it must not do is quietly grow the ability to
 * create accounts or delete anything -- that is the one line that must
 * never move without someone deciding to move it.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import SeedTopUp from './SeedTopUp'
import * as client from '@/api/client'

vi.mock('@/api/controlPlane', () => ({
  fetchDomainGuardStatus: () => Promise.resolve({ domain: 'x', protected: false }),
}))

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, runSeed: vi.fn() }
})

beforeEach(() => {
  vi.mocked(client.runSeed).mockReset()
  vi.mocked(client.runSeed).mockResolvedValue({ ok: true })
})

describe('gated like every other action that writes to a tenant', () => {
  it('will not run until the domain is typed', async () => {
    render(<SeedTopUp domain="src.example" />)
    expect(screen.getByRole('button', { name: 'Add more' })).toBeDisabled()
  })

  it('runs once something is typed', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
  })
})

describe('never creates accounts and never deletes anything', () => {
  it('always sends createUsers and reset as false', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [domain, , createUsers, reset] = vi.mocked(client.runSeed).mock.calls[0]
    expect(domain).toBe('src.example')
    expect(createUsers).toBe(false)
    expect(reset).toBe(false)
  })

  it('has no control that could create or delete users', () => {
    // Not "unchecked" -- ABSENT. A checkbox that exists and defaults off
    // can be turned on; these controls do not exist on this form at all.
    render(<SeedTopUp domain="src.example" />)
    expect(screen.queryByLabelText(/create users/i)).toBeNull()
    expect(screen.queryByLabelText(/create until full/i)).toBeNull()
    expect(screen.queryByLabelText(/all users/i)).toBeNull()
    expect(screen.queryByText(/reset/i)).toBeNull()
  })
})

describe('what it adds', () => {
  it('defaults to every service', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [, , , , opts] = vi.mocked(client.runSeed).mock.calls[0]
    expect(opts?.only).toBeUndefined()
  })

  it('narrows to one service when picked', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.change(screen.getByTestId('topup-only'), { target: { value: 'chat' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [, , , , opts] = vi.mocked(client.runSeed).mock.calls[0]
    expect(opts?.only).toBe('chat')
  })

  it('says plainly that it adds rather than replaces', () => {
    render(<SeedTopUp domain="src.example" />)
    expect(screen.getByText(/adds more content/i)).toBeInTheDocument()
    expect(screen.getByText(/left alone/i)).toBeInTheDocument()
  })
})
