/**
 * A job the queue started after the press said "queued", or one already running when the page
 * opened, was never watched: the Regression set card sat blank, with no log and no bar, through a
 * whole run (2026-11-01). The card attaches to its own job on mount.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import JobRunner from './JobRunner'
import * as client from '@/api/client'

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, fetchJob: vi.fn(), runAction: vi.fn() }
})

const SPEC = { label: 'Regression set', blurb: 'Four users.', destructive: false, confirm: '' }

beforeEach(() => { vi.mocked(client.fetchJob).mockReset() })

describe('a job already running when the card opens', () => {
  it('shows its log and its bar', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue({
      running: true, name: 'Regression set', rc: null, elapsed: 60,
      lines: ['[5/12] steps · esc: verify 1:1'], total: 1,
      progressPct: 41.7, etaSeconds: null, external: false,
    })
    render(<JobRunner name="regression" spec={SPEC} accountId={3} />)
    expect(await screen.findByText(/\[5\/12\] steps/)).toBeTruthy()
    expect(await screen.findByTestId('action-progress-regression')).toBeTruthy()
    expect(vi.mocked(client.fetchJob).mock.calls[0]).toEqual([0, '3', 'Regression set'])
  })

  it('stays quiet when nothing of its own is running', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue({
      running: false, name: '', rc: null, elapsed: 0, lines: [], total: 0,
      progressPct: null, etaSeconds: null, external: false,
    })
    render(<JobRunner name="regression" spec={SPEC} accountId={3} />)
    await waitFor(() => expect(client.fetchJob).toHaveBeenCalled())
    expect(screen.queryByTestId('action-progress-regression')).toBeNull()
  })
})
