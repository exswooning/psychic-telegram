import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import RunOptions from './RunOptions'
import * as client from '@/api/client'

const base = {
  dry_run: false, services: {}, rewrite_drive_links: false,
  mail_transport: 'engine' as const, delta_days: 2,
}

describe('RunOptions', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('shows who is carrying the mail, from the server', async () => {
    vi.spyOn(client, 'fetchToggles').mockResolvedValue({ ok: true, toggles: { ...base, mail_transport: 'split' } })
    render(<RunOptions />)
    await waitFor(() => expect(screen.getByTestId('transport-split')).toHaveAttribute('aria-pressed', 'true'))
  })

  it('renders the server\'s answer, not the request', async () => {
    // The server turns rewriting ON when you choose split. A control that
    // echoed the click would show it off.
    vi.spyOn(client, 'fetchToggles').mockResolvedValue({ ok: true, toggles: base })
    const patch = vi.spyOn(client, 'patchToggles').mockResolvedValue({
      ok: true,
      toggles: { ...base, mail_transport: 'split', rewrite_drive_links: true, last_note: 'turned rewriting on' },
    })
    render(<RunOptions />)
    await screen.findByTestId('transport-split')
    fireEvent.click(screen.getByTestId('transport-split'))

    expect(patch).toHaveBeenCalledWith({ mail_transport: 'split' })
    await waitFor(() => expect(screen.getByTestId('rewrite-drive-links')).toBeChecked())
    expect(screen.getByTestId('run-options-note')).toHaveTextContent('turned rewriting on')
  })

  it('scopes a run to named users, which Job control ticks do not', async () => {
    /* Job control's row ticks go to api_server's migrate/start. This field
       is the only thing that sets _RUN_STATE["users"], which is what scopes
       every action webui launches, including the delta pass. */
    vi.spyOn(client, 'fetchToggles').mockResolvedValue({ ok: true, toggles: base })
    const patch = vi.spyOn(client, 'patchToggles').mockResolvedValue({
      ok: true, toggles: { ...base, users: 'a@x.test' } })
    render(<RunOptions />)
    const box = await screen.findByTestId('run-users')
    fireEvent.change(box, { target: { value: ' a@x.test ' } })
    fireEvent.blur(box)
    await waitFor(() => expect(patch).toHaveBeenCalledWith({ users: 'a@x.test' }))
  })

  it('will not offer link rewriting under DMS, which cannot do it', async () => {
    vi.spyOn(client, 'fetchToggles').mockResolvedValue({ ok: true, toggles: { ...base, mail_transport: 'dms' } })
    render(<RunOptions />)
    await waitFor(() => expect(screen.getByTestId('rewrite-drive-links')).toBeDisabled())
  })

  it('sends only what changed, so one control cannot reset another', async () => {
    vi.spyOn(client, 'fetchToggles').mockResolvedValue({ ok: true, toggles: base })
    const patch = vi.spyOn(client, 'patchToggles').mockResolvedValue({ ok: true, toggles: { ...base, rewrite_drive_links: true } })
    render(<RunOptions />)
    await screen.findByTestId('rewrite-drive-links')
    fireEvent.click(screen.getByTestId('rewrite-drive-links'))
    expect(patch).toHaveBeenCalledWith({ rewrite_drive_links: true })
  })
})
