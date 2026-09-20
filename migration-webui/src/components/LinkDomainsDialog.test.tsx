/**
 * Linking two domains can succeed and still be a mistake worth flagging:
 * a target with far fewer users than the source is at real risk of hitting
 * Google's own "Domain user limit reached" wall partway through a
 * migration. That happened live -- three hours in, after the run had
 * already started writing data -- because the only place this was ever
 * surfaced was a warning log line nobody was watching. The API now returns
 * that warning inside a successful link's own detail string; this pins
 * that the dialog actually surfaces it instead of closing on a clean
 * success like it always used to.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import LinkDomainsDialog from './LinkDomainsDialog'

const allDomains = vi.fn()
const linkDomains = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchAllDomains: () => allDomains(),
  linkDomains: (...a: unknown[]) => linkDomains(...a),
}))

beforeEach(() => {
  allDomains.mockReset(); linkDomains.mockReset()
  allDomains.mockResolvedValue({ domains: [
    { accountId: 1, accountEmail: 'a@ex.com', side: 'source', domain: 'src.example',
      adminEmail: 'admin@src.example', hasKey: true, clientId: '111' },
    { accountId: 2, accountEmail: 'b@ex.com', side: 'target', domain: 'tgt.example',
      adminEmail: 'admin@tgt.example', hasKey: true, clientId: '222' },
  ] })
})

const pickAndConnect = async () => {
  fireEvent.change(await screen.findByTestId('link-source'),
                   { target: { value: '1:source:' } })
  fireEvent.change(screen.getByTestId('link-target'),
                   { target: { value: '2:target:' } })
  fireEvent.change(screen.getByTestId('link-reason'),
                   { target: { value: 'test run' } })
  fireEvent.click(screen.getByTestId('link-connect'))
}

describe('LinkDomainsDialog licence-headroom warning', () => {
  it('stays open and shows the warning on a successful-but-risky link', async () => {
    linkDomains.mockResolvedValue({
      ok: true,
      detail: 'src.example -> tgt.example  ⚠ src.example has 300 user(s); '
        + 'tgt.example currently has only 5.',
    })
    const onLinked = vi.fn(); const onClose = vi.fn()
    render(<MemoryRouter>
      <LinkDomainsDialog open onClose={onClose} onLinked={onLinked} />
    </MemoryRouter>)

    await pickAndConnect()

    expect(await screen.findByTestId('link-warning')).toHaveTextContent('300 user(s)')
    // The link already happened -- the caller's own list must refresh --
    // but the dialog itself waits for an explicit Close, not an auto-close.
    expect(onLinked).toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('link-close')).toBeInTheDocument()
  })

  it('closes immediately like before when there is nothing to warn about', async () => {
    linkDomains.mockResolvedValue({ ok: true, detail: 'src.example -> tgt.example' })
    const onLinked = vi.fn(); const onClose = vi.fn()
    render(<MemoryRouter>
      <LinkDomainsDialog open onClose={onClose} onLinked={onLinked} />
    </MemoryRouter>)

    await pickAndConnect()

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(onLinked).toHaveBeenCalled()
    expect(screen.queryByTestId('link-warning')).toBeNull()
  })

  it('shows the real error, not a warning, when linking fails outright', async () => {
    linkDomains.mockResolvedValue({ ok: false, detail: 'target has no key on file' })
    const onLinked = vi.fn(); const onClose = vi.fn()
    render(<MemoryRouter>
      <LinkDomainsDialog open onClose={onClose} onLinked={onLinked} />
    </MemoryRouter>)

    await pickAndConnect()

    expect(await screen.findByTestId('link-error'))
      .toHaveTextContent('target has no key on file')
    expect(onLinked).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.queryByTestId('link-warning')).toBeNull()
  })
})
