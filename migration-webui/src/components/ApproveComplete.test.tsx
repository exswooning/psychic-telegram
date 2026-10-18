import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ApproveComplete from './ApproveComplete'

const approve = vi.fn()
const undo = vi.fn()
const lifecycle = vi.fn()
const restore = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  restoreProjectCreation: (...a: unknown[]) => restore(...a),
  fetchGcloudIdentities: () => Promise.resolve({ identities: [
    { config: '/tmp/cloudsdk-x', accounts: ['old-admin@t.example'] }] }),
  fetchLifecycle: () => lifecycle(),
  approveMigrationComplete: (...a: unknown[]) => approve(...a),
  undoApproval: (...a: unknown[]) => undo(...a),
}))

const view = (state = {}) => ({
  accountId: 5, state, teardownDays: 30,
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
    expect(await screen.findByText(/Nothing is approved until someone presses/)).toBeTruthy()
    const plan = await screen.findByTestId('teardown-plan')
    expect(plan).toHaveTextContent('delete project p-src, revoke delegation 111')
    expect(plan).toHaveTextContent('leave project p-shared')
    expect(plan).toHaveTextContent('login kept (info@client.example)')
  })

  it('shows an approval with its countdown, and takes it back', async () => {
    lifecycle.mockResolvedValue(view({ approved_at: '2026-10-04T00:00:00Z', approved_by: 'boss@x.com',
      teardown_due_at: new Date(Date.now() + 3 * 86_400_000).toISOString() }))
    undo.mockResolvedValue({ ok: true, actionId: 1, detail: 'approval taken back' })
    render(<ApproveComplete />)
    expect(await screen.findByTestId('teardown-due')).toHaveTextContent(/by boss@x.com.*in 3 days/)
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


/** Live: dharan.gov.np forbids creating Cloud projects; setup turns it on for the
 *  migration, and it is turned back off once the migration is complete. */
describe('project creation setup turned on', () => {
  it('asks for the password once when no login was kept, and turns it back off', async () => {
    lifecycle.mockResolvedValue({ ...view(), projectCreation: [{ side: 'source', domain: 'dharan.gov.np',
      admin: 'it.department@dharan.gov.np', turnedOnAt: '2026-10-18T13:40:00Z', unattended: false }] })
    restore.mockResolvedValue({ ok: true, actionId: 1, detail: 'turning project creation back off (source)' })
    render(<ApproveComplete />)
    expect(await screen.findByTestId('project-creation-source')).toHaveTextContent('Turn it back off once the migration is complete')
    const off = screen.getByTestId('project-creation-source-off')
    expect(off).toBeDisabled()
    fireEvent.change(screen.getByTestId('project-creation-source-password'), { target: { value: 'pw' } })
    fireEvent.click(off)
    await waitFor(() => expect(restore).toHaveBeenCalledWith('source', 'pw', 'project_creation'))
  })

  it('says approval will do it when a login is kept', async () => {
    lifecycle.mockResolvedValue({ ...view(), projectCreation: [{ side: 'target', domain: 'acme.example',
      admin: 'admin@acme.example', turnedOnAt: '2026-10-18T13:40:00Z', unattended: true }] })
    render(<ApproveComplete />)
    expect(await screen.findByTestId('project-creation-target'))
      .toHaveTextContent('turned back off when you approve the migration as complete')
    expect(screen.queryByTestId('project-creation-target-password')).toBeNull()
  })
})

describe('Drive SDK access setup turned on', () => {
  it('is listed and turned back off like project creation', async () => {
    lifecycle.mockResolvedValue({ ...view(), projectCreation: [{ side: 'source', domain: 'dharan.gov.np',
      admin: 'it.department@dharan.gov.np', turnedOnAt: '2026-10-19T03:00:00Z', unattended: false,
      setting: 'drive_sdk', name: 'Drive SDK access' }] })
    restore.mockResolvedValue({ ok: true, actionId: 1, detail: 'turning drive sdk back off (source)' })
    render(<ApproveComplete />)
    expect(await screen.findByTestId('drive-sdk-source')).toHaveTextContent('Drive SDK access on dharan.gov.np')
    fireEvent.change(screen.getByTestId('drive-sdk-source-password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByTestId('drive-sdk-source-off'))
    await waitFor(() => expect(restore).toHaveBeenCalledWith('source', 'pw', 'drive_sdk'))
  })
})
