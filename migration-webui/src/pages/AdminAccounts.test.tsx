import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import AdminAccounts from './AdminAccounts'

const fetchAdminAccounts = vi.fn()
const deleteAccount = vi.fn()
const createAccount = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  createAccount: (...a: unknown[]) => createAccount(...a),
  fetchAdminAccounts: (...a: unknown[]) => fetchAdminAccounts(...a),
  deleteAccount: (...a: unknown[]) => deleteAccount(...a),
  setAccountSubscription: vi.fn(),
  setAccountSeedEnabled: vi.fn(),
}))

const acct = (id: number, email: string, is_superadmin = false) => ({
  id, email, name: 'N', plan: 'trial', created_at: '2026-10-01T00:00:00Z',
  is_superadmin, subscription_active: true, seed_enabled: false })

describe('deleting a throwaway account', () => {
  beforeEach(() => {
    fetchAdminAccounts.mockReset(); deleteAccount.mockReset()
    fetchAdminAccounts.mockResolvedValue([acct(1, 'boss@x.com', true), acct(7, 'throwaway@x.com')])
  })

  it('offers no delete for a superadmin', async () => {
    render(<AdminAccounts />)
    await screen.findByTestId('delete-account-7')
    expect(screen.queryByTestId('delete-account-1')).toBeNull()
  })

  it('needs the email typed back, then deletes', async () => {
    deleteAccount.mockResolvedValue({ ok: true, actionId: 1, detail: 'deleted' })
    render(<AdminAccounts />)
    fireEvent.click(await screen.findByTestId('delete-account-7'))
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'test account' } })
    const confirm = screen.getByRole('button', { name: 'Confirm' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: 'throwaway@x.com' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(deleteAccount).toHaveBeenCalledWith(7, 'throwaway@x.com', 'test account'))
  })
})

describe('creating an account for a new pair', () => {
  it('sends the sign-in, name and password with a reason, then says what to do next', async () => {
    fetchAdminAccounts.mockResolvedValue([acct(1, 'boss@x.com', true)])
    createAccount.mockResolvedValue({ ok: true, actionId: 2, detail: 'created account 9 (client@x.com)' })
    render(<AdminAccounts />)
    const go = await screen.findByTestId('new-account-go')
    expect(go).toBeDisabled()
    fireEvent.change(screen.getByTestId('new-account-email'), { target: { value: 'client@x.com' } })
    fireEvent.change(screen.getByTestId('new-account-name'), { target: { value: 'Client pair' } })
    fireEvent.change(screen.getByTestId('new-account-password'), { target: { value: 'long-enough-1' } })
    fireEvent.click(go)
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'new client' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(createAccount).toHaveBeenCalledWith('client@x.com', 'long-enough-1', 'Client pair', 'new client'))
    expect(await screen.findByText(/Sign in as it to set up its pair/)).toBeTruthy()
  })
})
