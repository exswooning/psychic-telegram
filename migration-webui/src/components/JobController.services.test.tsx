import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import JobController from './JobController'
import * as client from '@/api/client'

const startMigration = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  startMigration: (...a: unknown[]) => startMigration(...a),
  stopJob: vi.fn(),
}))

const toggles = (services: Record<string, boolean>, mail_transport = 'engine') =>
  ({ ok: true, toggles: { dry_run: false, services, mail_transport } } as never)

const users = [{ source_email: 'a@x.test', target_email: 'a@y.test',
                 status: 'PENDING', services_done: '', itemsDone: 0,
                 itemsFailed: 0, itemsSkipped: 0, percent: 0 }] as never

describe('JobController service scope', () => {
  beforeEach(() => { vi.restoreAllMocks(); startMigration.mockReset() })

  it('migrates what the service switches actually say', async () => {
    /* It sent ['drive'] hardcoded, so turning Gmail on and pressing Migrate
       copied no mail and said nothing about it. */
    vi.spyOn(client, 'fetchToggles').mockResolvedValue(
      toggles({ drive: false, gmail: true, calendar: true, chat: false }))
    startMigration.mockResolvedValue({ ok: true })
    render(<JobController users={users} nodes={[]} />)

    const go = await screen.findByTestId('migrate-selected')
    await waitFor(() => expect(go).not.toBeDisabled())
    fireEvent.click(go)
    const box = document.querySelector('[role="dialog"] input, [role="dialog"] textarea')
    fireEvent.change(box!, { target: { value: 'why' } })
    fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(startMigration).toHaveBeenCalled())
    expect(startMigration.mock.calls[0][1]).toEqual(['gmail', 'calendar'])
  })

  it('leaves the mail to DMS when DMS is carrying it', async () => {
    /* Same rule webui applies when it launches. The duplicate this prevents
       is one a person sees in their own inbox. */
    vi.spyOn(client, 'fetchToggles').mockResolvedValue(
      toggles({ drive: true, gmail: true }, 'dms'))
    startMigration.mockResolvedValue({ ok: true })
    render(<JobController users={users} nodes={[]} />)

    const go = await screen.findByTestId('migrate-selected')
    await waitFor(() => expect(go).not.toBeDisabled())
    fireEvent.click(go)
    const box = document.querySelector('[role="dialog"] input, [role="dialog"] textarea')
    fireEvent.change(box!, { target: { value: 'why' } })
    fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(startMigration).toHaveBeenCalled())
    expect(startMigration.mock.calls[0][1]).toEqual(['drive'])
  })

  it('will not launch with every service off', async () => {
    /* An empty --services burned a capacity slot to do nothing and reported
       a clean run. */
    vi.spyOn(client, 'fetchToggles').mockResolvedValue(
      toggles({ drive: false, gmail: false }))
    render(<JobController users={users} nodes={[]} />)
    await waitFor(() => expect(screen.getByTestId('migrate-selected')).toBeDisabled())
  })
})
