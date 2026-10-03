import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import AdminAccounts from './AdminAccounts'

const fetchAdminAccounts = vi.fn()
const deleteAccount = vi.fn()
vi.mock('@/api/controlPlane', () => ({
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
