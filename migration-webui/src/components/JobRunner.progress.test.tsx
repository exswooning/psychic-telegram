/** Other services' jobs show how far they are, from the job's own [done/total]. */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import JobRunner from './JobRunner'
import * as client from '@/api/client'

vi.mock('@/hooks/useAccountPair', () => ({ useAccountPair: () => '' }))
vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, fetchJob: vi.fn(), runAction: vi.fn() }
})

const SPEC = { label: 'shared drives migrate', blurb: 'Copy every shared drive.', destructive: false, confirm: '' }
const job = (over: Partial<client.JobStatus>) => ({
  running: true, name: 'shared drives migrate', rc: null, elapsed: 60, lines: [], total: 0,
  progressPct: null, etaSeconds: null, external: false, ...over,
}) as client.JobStatus

const start = async () => {
  // Nothing of this panel's is running when it opens; the press starts it (the card now
  // attaches on mount to a job already running, so the first look must see none).
  vi.mocked(client.fetchJob).mockResolvedValueOnce({ running: false, name: '', rc: null, elapsed: 0, lines: [], total: 0, progressPct: null, etaSeconds: null, external: false } as never)
  render(<JobRunner name="shared_drives_migrate" spec={SPEC} />)
  ;(await screen.findByRole('button', { name: /shared drives migrate/i })).click()
  await waitFor(() => expect(client.runAction).toHaveBeenCalled())
}

describe('the progress bar', () => {
  beforeEach(() => {
    vi.mocked(client.runAction).mockReset().mockResolvedValue({ ok: true, error: null })
    vi.mocked(client.fetchJob).mockReset()
  })

  it('shows the percentage and time left from the job counter', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue(job({ progressPct: 40, etaSeconds: 300 }))
    await start()
    const bar = await screen.findByTestId('action-progress-shared_drives_migrate')
    await waitFor(() => expect(bar).toHaveTextContent('40% · about 5 min left'))
    expect(bar.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('40')
  })

  it('without a counter, says it is working but invents no number', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue(job({}))
    await start()
    const bar = await screen.findByTestId('action-progress-shared_drives_migrate')
    expect(bar.textContent).not.toMatch(/%/)
    expect(bar.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBeNull()
  })
})
