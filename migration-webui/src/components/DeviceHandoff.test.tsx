/** Live: Google asked the server's browser for a phone number while the admin sat
 *  signed in on the operator's own Mac. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import DeviceHandoff from './DeviceHandoff'

const send = vi.hoisted(() => vi.fn(() => Promise.resolve({ ok: true, actionId: 1, detail: '' })))
const review = vi.hoisted(() => vi.fn(() => Promise.resolve({ ok: true, actionId: 2, detail: 'Review recorded' })))
vi.mock('@/api/controlPlane', () => ({ sendFullSetupCode: send, sendFullSetupNotice: review }))

describe('a sign-in handed to this device', () => {
  it('opens the sign-in here and sends back the code Google shows', async () => {
    render(<DeviceHandoff side="source"
                          handoff={{ kind: 'signin', url: 'https://accounts.google.com/o/oauth2/auth?x=1' }} />)
    expect(screen.getByTestId('handoff-source-open'))
      .toHaveAttribute('href', 'https://accounts.google.com/o/oauth2/auth?x=1')
    fireEvent.change(screen.getByTestId('handoff-source-code'), { target: { value: ' 4/0AbCd-ef_12 ' } })
    fireEvent.click(screen.getByTestId('handoff-source-send'))
    await waitFor(() => expect(send).toHaveBeenCalledWith('source', '4/0AbCd-ef_12'))
    expect(await screen.findByText(/continues on its own/)).toBeInTheDocument()
  })

  it('gives the delegation as a link, with the client ID and scopes to paste if it opens empty', () => {
    render(<DeviceHandoff side="target" handoff={{
      kind: 'dwd', url: 'https://admin.google.com/ac/owl/domainwidedelegation?clientIdToAdd=123',
      clientId: '123', scopes: 'https://www.googleapis.com/auth/drive' }} />)
    expect(screen.getByTestId('handoff-target-open')).toHaveAttribute('target', '_blank')
    expect(screen.getByDisplayValue('123')).toBeInTheDocument()
    expect(screen.queryByTestId('handoff-target-code')).toBeNull()
  })

  it('is not there when the server is doing it itself', () => {
    const { container } = render(<DeviceHandoff side="source" handoff={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  const notice = { kind: 'review' as const, requestId: 'a'.repeat(48), noticeHash: 'b'.repeat(64),
    adminEmail: 'nest@target.example', expiresAt: Date.now() / 1000 + 600,
    title: 'Review Google account notice', text: 'Administrator access and account terms',
    category: 'workspace_welcome' }

  it('waits for an explicit account-specific review before submitting approval', async () => {
    review.mockClear()
    render(<DeviceHandoff side="target" handoff={notice} />)
    expect(screen.getByText(notice.text)).toBeInTheDocument()
    const approve = screen.getByRole('button', { name: 'Approve and continue' })
    expect(approve).toBeDisabled()
    expect(review).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(approve)
    await waitFor(() => expect(review).toHaveBeenCalledWith('target', notice.requestId, notice.noticeHash, 'approve'))
    await screen.findByText('Response sent. Waiting for Google to confirm progress.')
    expect(approve).toBeDisabled()
    expect(screen.queryByText(/Setup continues on its own/)).toBeNull()
  })

  it('requires fresh review if the server presents a different notice', () => {
    const { rerender } = render(<DeviceHandoff side="target" handoff={notice} />)
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: 'Approve and continue' })).toBeEnabled()
    rerender(<DeviceHandoff side="target" handoff={{ ...notice, requestId: 'c'.repeat(48) }} />)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(screen.getByRole('button', { name: 'Approve and continue' })).toBeDisabled()
  })

  it('prevents expired approvals and old device codes', () => {
    const { rerender } = render(<DeviceHandoff side="target" handoff={{ ...notice, expiresAt: 1 }} />)
    expect(screen.getByRole('button', { name: 'Approve and continue' })).toBeDisabled()
    expect(screen.getByText(/This request expired/)).toBeInTheDocument()
    rerender(<DeviceHandoff side="target" handoff={{ kind: 'signin', url: 'https://accounts.google.com/', expiresAt: 1 }} />)
    expect(screen.getByTestId('handoff-target-send')).toBeDisabled()
    expect(screen.getByText(/old codes cannot resume it/)).toBeInTheDocument()
  })

  it('lets the operator decline without first approving the notice', async () => {
    review.mockClear()
    render(<DeviceHandoff side="target" handoff={notice} />)
    fireEvent.click(screen.getByRole('button', { name: 'Decline this step' }))
    await waitFor(() => expect(review).toHaveBeenCalledWith('target', notice.requestId, notice.noticeHash, 'decline'))
  })

  it('does not apply a late response to a new request', async () => {
    let finish!: (value: { ok: boolean; actionId: number; detail: string }) => void
    review.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { rerender } = render(<DeviceHandoff side="target" handoff={notice} />)
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Approve and continue' }))
    rerender(<DeviceHandoff side="target" handoff={{ ...notice, requestId: 'd'.repeat(48) }} />)
    await act(async () => finish({ ok: true, actionId: 2, detail: 'Old response' }))
    expect(screen.queryByText('Old response')).toBeNull()
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: 'Approve and continue' })).toBeEnabled()
  })
})
