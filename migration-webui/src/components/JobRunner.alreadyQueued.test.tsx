/**
 * Pressing a launch button twice while the account's one job slot is held answers the second
 * press "... is already queued for this account". That is the first press working, so it is
 * shown as information beside the queue -- not as a red error that reads like a failed launch
 * (live, the Regression set card: the second press looked like a failure).
 */
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import JobRunner from './JobRunner'
import * as client from '@/api/client'

vi.mock('@/hooks/useAccountPair', () => ({ useAccountPair: () => '' }))
vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, fetchJob: vi.fn(), runAction: vi.fn() }
})

const SPEC = { label: 'Regression set', blurb: 'Four users.', destructive: false, confirm: '' }

beforeEach(() => {
  vi.mocked(client.fetchJob).mockResolvedValue({
    running: false, name: '', rc: null, elapsed: 0, lines: [], total: 0,
    progressPct: null, etaSeconds: null, external: false,
  })
})

describe('a second press while queued', () => {
  it('reads as information, not an error', async () => {
    vi.mocked(client.runAction).mockResolvedValue(
      { ok: false, error: 'Regression set is already queued for this account' })
    render(<JobRunner name="regression" spec={SPEC} />)
    ;(await screen.findByRole('button', { name: /regression set/i })).click()
    expect((await screen.findByTestId('action-queued-regression')).textContent)
      .toMatch(/already queued for this account/)
    expect(screen.queryByTestId('action-error-regression')).toBeNull()
  })

  it('still shows a real refusal as an error', async () => {
    vi.mocked(client.runAction).mockResolvedValue({ ok: false, error: 'subscription inactive' })
    render(<JobRunner name="regression" spec={SPEC} />)
    ;(await screen.findByRole('button', { name: /regression set/i })).click()
    expect((await screen.findByTestId('action-error-regression')).textContent).toMatch(/subscription inactive/)
    expect(screen.queryByTestId('action-queued-regression')).toBeNull()
  })
})
