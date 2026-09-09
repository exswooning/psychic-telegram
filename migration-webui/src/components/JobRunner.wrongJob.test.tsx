/**
 * A read-only "count files per shared drive" panel displayed this, live,
 * with a Stop button beside it:
 *
 *     [78/200] r2-seeduser167@...: 1 files, 1390 messages, ... deleted
 *     ... still deleting: 85/200 users done after 45m00s (17 in parallel)
 *
 * Nothing was deleting. It was the reset phase of a running eleven-hour
 * seed, streamed under another panel's heading, because JobRunner asked
 * /api/job for "whatever is running" and there is exactly one Job per
 * account. webui's _job_snapshot has guarded against this since it once
 * reported a 298,185-item deletion as a job that had been destroyed -- but
 * the guard only engages when the caller says which job it means, and this
 * caller never did.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import JobRunner from './JobRunner'
import * as client from '@/api/client'

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, fetchJob: vi.fn(), runAction: vi.fn() }
})

const SPEC = {
  label: 'shared drives inventory', blurb: 'Count files per shared drive.',
  destructive: false, confirm: '',
}

beforeEach(() => {
  vi.mocked(client.runAction).mockReset()
  vi.mocked(client.fetchJob).mockReset()
  vi.mocked(client.runAction).mockResolvedValue({ ok: true, error: null })
  vi.mocked(client.fetchJob).mockResolvedValue({
    running: false, name: '', rc: null, elapsed: 0, lines: [], total: 0,
    progressPct: null, etaSeconds: null, external: false,
  })
})

const start = async () => {
  render(<JobRunner name="shared_drives_inventory" spec={SPEC} />)
  const btn = await screen.findByRole('button', { name: /shared drives inventory/i })
  btn.click()
  await waitFor(() => expect(client.runAction).toHaveBeenCalled())
}

describe('it asks for its own job, by name', () => {
  it('passes the label the job is started under', async () => {
    await start()
    await waitFor(() => expect(client.fetchJob).toHaveBeenCalled())
    const args = vi.mocked(client.fetchJob).mock.calls[0]
    expect(args[2]).toBe('shared drives inventory')
  })

  it('never asks for "whatever is running"', async () => {
    /* That question has exactly one answer per account, and it is usually
       somebody else's job. */
    await start()
    await waitFor(() => expect(client.fetchJob).toHaveBeenCalled())
    for (const call of vi.mocked(client.fetchJob).mock.calls)
      expect(call[2]).toBeTruthy()
  })
})

describe('when another job holds the tenant', () => {
  const SEED_IS_RUNNING = {
    running: false, name: 'shared drives inventory', rc: null, elapsed: 0,
    lines: [], total: 0, progressPct: null, etaSeconds: null,
    external: false, requested: 'shared drives inventory', unknown: true,
    now_running: 'seed',
  }

  it('shows no output from it', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue(SEED_IS_RUNNING as never)
    await start()
    await waitFor(() =>
      expect(screen.queryByText(/still deleting/)).toBeNull())
  })

  it('offers no Stop, because there is nothing of ours to stop', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue(SEED_IS_RUNNING as never)
    await start()
    await waitFor(() => expect(screen.queryByText(/^Stop$/)).toBeNull())
  })

  it('says WHAT is running, instead of looking broken', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue(SEED_IS_RUNNING as never)
    await start()
    expect(await screen.findByTestId('blocked-shared_drives_inventory'))
      .toHaveTextContent(/seed is running on this tenant/i)
  })
})

describe('when it is genuinely this panel running', () => {
  it('its own lines are shown', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue({
      running: true, name: 'shared drives inventory', rc: null, elapsed: 3,
      lines: ['drive A: 12 files'], total: 1, progressPct: null,
      etaSeconds: null, external: false, requested: 'shared drives inventory',
    } as never)
    await start()
    expect(await screen.findByText(/drive A: 12 files/)).toBeInTheDocument()
  })

  it('and no blocked notice', async () => {
    vi.mocked(client.fetchJob).mockResolvedValue({
      running: true, name: 'shared drives inventory', rc: null, elapsed: 3,
      lines: ['drive A: 12 files'], total: 1, progressPct: null,
      etaSeconds: null, external: false,
    } as never)
    await start()
    await waitFor(() => expect(screen.queryByText(/is running on this tenant/)).toBeNull())
  })
})
