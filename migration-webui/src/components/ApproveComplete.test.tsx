import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ApproveComplete from './ApproveComplete'

const approve = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchGcloudIdentities: () => Promise.resolve({ identities: [
    { config: '/tmp/cloudsdk-x', accounts: ['old-admin@t.example'] }] }),
  approveMigrationComplete: (...a: unknown[]) => approve(...a),
}))

describe('approving a migration as complete', () => {
  it('shows who gcloud holds, then signs them out with a reason', async () => {
    approve.mockResolvedValue({ ok: true, actionId: 1, detail: 'approved; gcloud signed out of: old-admin@t.example' })
    render(<ApproveComplete />)
    expect(await screen.findByText('old-admin@t.example')).toBeTruthy()
    fireEvent.click(screen.getByTestId('approve-complete-go'))
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'client signed off' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('client signed off'))
    expect(await screen.findByText(/signed out of: old-admin/)).toBeTruthy()
  })
})
