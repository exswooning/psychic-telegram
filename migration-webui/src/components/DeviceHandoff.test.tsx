/** Live: Google asked the server's browser for a phone number while the admin sat
 *  signed in on the operator's own Mac. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import DeviceHandoff from './DeviceHandoff'

const send = vi.hoisted(() => vi.fn(() => Promise.resolve({ ok: true, actionId: 1, detail: '' })))
vi.mock('@/api/controlPlane', () => ({ sendFullSetupCode: send }))

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
})
