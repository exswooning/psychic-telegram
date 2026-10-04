import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ApproveComplete from './ApproveComplete'

const approve = vi.fn()
const undo = vi.fn()
const lifecycle = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchGcloudIdentities: () => Promise.resolve({ identities: [
    { config: '/tmp/cloudsdk-x', accounts: ['old-admin@t.example'] }] }),
  fetchLifecycle: () => lifecycle(),
  approveMigrationComplete: (...a: unknown[]) => approve(...a),
  undoApproval: (...a: unknown[]) => undo(...a),
}))

const view = (state = {}) => ({
  accountId: 5, state, autoApproveDays: 30, teardownDays: 30,
  plan: [
    { side: 'source', domain: 'client.example', project: 'p-src', clientId: '111', keyFile: 'k',
      loginKept: true, adminEmail: 'info@client.example', leftBecause: '' },
    { side: 'target', domain: 'tgt.example', project: 'p-shared', clientId: '222', keyFile: 'k2',
      loginKept: false, adminEmail: '', leftBecause: 'project p-shared holds the key of tgt.example (target, account 3)' },
  ] })

describe('approving a migration as complete', () => {
  it('says what the teardown will delete and what it leaves, before it happens', async () => {
    lifecycle.mockResolvedValue(view())
    render(<ApproveComplete />)
    const plan = await screen.findByTestId('teardown-plan')
    expect(plan).toHaveTextContent('delete project p-src, revoke delegation 111')
    expect(plan).toHaveTextContent('leave project p-shared')
    expect(plan).toHaveTextContent('login kept (info@client.example)')
  })

  it('shows an approval with its countdown, and takes it back', async () => {
    lifecycle.mockResolvedValue(view({ approved_at: '2026-10-04T00:00:00Z', approved_by: 'auto',
      teardown_due_at: new Date(Date.now() + 3 * 86_400_000).toISOString() }))
    undo.mockResolvedValue({ ok: true, actionId: 1, detail: 'approval taken back' })
    render(<ApproveComplete />)
    expect(await screen.findByTestId('teardown-due')).toHaveTextContent(/automatically.*in 3 days/)
    fireEvent.click(screen.getByTestId('undo-approval'))
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'rerun wanted' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(undo).toHaveBeenCalledWith('rerun wanted'))
  })

  it('shows who gcloud holds, then signs them out with a reason', async () => {
    lifecycle.mockResolvedValue(view())
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
