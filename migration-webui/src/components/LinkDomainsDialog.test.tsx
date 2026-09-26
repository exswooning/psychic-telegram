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

/*
 * The screenshot: link source -> target, get the licence warning (300 users
 * against 201 seats), then change the TARGET to a different tenant. The dialog
 * stayed on "Close" with the old pair's warning over it and no way to link the
 * new one. The warning describes the pair that was linked, so it must go when
 * that pair is no longer what is selected.
 */
describe('changing the pair after a warning', () => {
  const three = () => allDomains.mockResolvedValue({ domains: [
    { accountId: 1, accountEmail: 'a@ex.com', side: 'source', domain: 'src.example',
      adminEmail: 'admin@src.example', hasKey: true, clientId: '111' },
    { accountId: 2, accountEmail: 'b@ex.com', side: 'target', domain: 'tgt.example',
      adminEmail: 'admin@tgt.example', hasKey: true, clientId: '222' },
    { accountId: 3, accountEmail: 'c@ex.com', side: 'target', domain: 'tgt2.example',
      adminEmail: 'admin@tgt2.example', hasKey: true, clientId: '333' },
  ] })
  const warnFirst = () => linkDomains.mockResolvedValueOnce({
    ok: true, detail: 'src.example -> tgt.example  ⚠ src.example has 300 user(s); tgt.example currently has only 201.' })
  const setup = async () => {
    three(); warnFirst()
    render(<MemoryRouter><LinkDomainsDialog open onClose={vi.fn()} onLinked={vi.fn()} /></MemoryRouter>)
    await pickAndConnect()
    await screen.findByTestId('link-warning')
  }
  const changeTarget = (v: string) =>
    fireEvent.change(screen.getByTestId('link-target'), { target: { value: v } })

  it('drops the old pair\'s warning and offers to link again when the target changes', async () => {
    await setup()
    expect(screen.queryByTestId('link-connect')).toBeNull()          // the locked state
    changeTarget('3:target:')
    expect(screen.queryByTestId('link-warning')).toBeNull()
    expect(screen.getByTestId('link-connect')).toBeEnabled()
    expect(screen.queryByTestId('link-close')).toBeNull()
  })

  it('links the newly chosen pair, not the one that was warned about', async () => {
    await setup()
    changeTarget('3:target:')
    linkDomains.mockResolvedValueOnce({ ok: true, detail: 'src.example -> tgt2.example' })
    fireEvent.click(screen.getByTestId('link-connect'))
    await waitFor(() => expect(linkDomains).toHaveBeenCalledTimes(2))
    expect(linkDomains.mock.calls[1][2]).toMatchObject({ accountId: 3, side: 'target' })
  })

  it('brings the warning back if the selection returns to the pair it was about', async () => {
    await setup()
    changeTarget('3:target:')
    changeTarget('2:target:')
    expect(await screen.findByTestId('link-warning')).toHaveTextContent('300 user(s)')
    expect(screen.getByTestId('link-close')).toBeInTheDocument()
  })

  it('changing the source also releases it', async () => {
    three(); warnFirst()
    allDomains.mockResolvedValue({ domains: [
      { accountId: 1, accountEmail: 'a@ex.com', side: 'source', domain: 'src.example', adminEmail: 'x', hasKey: true, clientId: '1' },
      { accountId: 4, accountEmail: 'd@ex.com', side: 'source', domain: 'src2.example', adminEmail: 'x', hasKey: true, clientId: '4' },
      { accountId: 2, accountEmail: 'b@ex.com', side: 'target', domain: 'tgt.example', adminEmail: 'x', hasKey: true, clientId: '2' },
    ] })
    render(<MemoryRouter><LinkDomainsDialog open onClose={vi.fn()} onLinked={vi.fn()} /></MemoryRouter>)
    await pickAndConnect(); await screen.findByTestId('link-warning')
    fireEvent.change(screen.getByTestId('link-source'), { target: { value: '4:source:' } })
    expect(screen.getByTestId('link-connect')).toBeEnabled()
  })

  it('a failed attempt\'s error does not outlive the selection it was about', async () => {
    three()
    linkDomains.mockResolvedValueOnce({ ok: false, detail: 'target has no key on file' })
    render(<MemoryRouter><LinkDomainsDialog open onClose={vi.fn()} onLinked={vi.fn()} /></MemoryRouter>)
    await pickAndConnect()
    await screen.findByTestId('link-error')
    changeTarget('3:target:')
    expect(screen.queryByTestId('link-error')).toBeNull()
  })
})
