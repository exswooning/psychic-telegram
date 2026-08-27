import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import RunningNow from './RunningNow'

/**
 * Two things this page got wrong at once, both seen live.
 *
 * It showed a migration that had finished 26 hours earlier -- the fleet
 * node had stopped heartbeating and kept its last active_job forever, so
 * the row survived its own process, complete with a Stop button for a pid
 * that no longer existed.
 *
 * And it showed nothing at all for a seed that was plainly running,
 * because that job's admission row had been reaped and the own-job branch
 * required one. A running job with no row is still a running job.
 */
// vi.hoisted: vi.mock is lifted above every const in this file, so a
// factory closing over a plain declaration throws "cannot access before
// initialization" at import time.
const cp = vi.hoisted(() => ({
  fetchTenantConfigStatus: vi.fn(), fetchFullSetupStatus: vi.fn(),
  fetchFleet: vi.fn(), fetchActiveJobs: vi.fn(), fetchMe: vi.fn(),
  stopJob: vi.fn(), fetchProvisionStatus: vi.fn(),
}))
const client = vi.hoisted(() => ({ fetchJob: vi.fn(), stopJob: vi.fn() }))

vi.mock('@/api/controlPlane', () => cp)
vi.mock('@/api/client', () => client)

const node = (over = {}) => ({
  node_id: 'vps-garud', hostname: 'aryan-vps-garud-migration', location: null,
  code_commit: null, last_seen: '2026-08-26T09:06:20Z', cpu_pct: 27.5,
  ram_pct: 39.4, disk_pct: 61.7, active_job: 'migrate', job_pid: 2688728,
  transfer_mode: 'server_side', users_done: 0, users_running: 0,
  users_failed: 0, error_rate: 0, healthy: true, secondsSinceHeartbeat: 5,
  ...over,
})

describe('Running Now', () => {
  beforeEach(() => {
    Object.values(cp).forEach((f) => f.mockReset())
    client.fetchJob.mockReset()
    cp.fetchTenantConfigStatus.mockResolvedValue(null)
    cp.fetchFullSetupStatus.mockResolvedValue(null)
    cp.fetchProvisionStatus.mockResolvedValue(null)
    cp.fetchMe.mockResolvedValue({ id: 66 })
    cp.fetchFleet.mockResolvedValue([])
    cp.fetchActiveJobs.mockResolvedValue([])
    client.fetchJob.mockResolvedValue({ running: false, name: '', lines: [] })
  })

  it('lists a running job that has no admission row', async () => {
    // The row is gone whenever the process outlived the restart that
    // forgot it. The server can still see and name the job.
    client.fetchJob.mockResolvedValue({
      running: true, name: 'seed', external: true, elapsed: 1928,
      progressPct: 3, lines: ['[5/201] george@source.example: 4292 messages'],
    })
    render(<RunningNow />)
    await waitFor(() => expect(screen.getByText('seed')).toBeTruthy())
  })

  it('says such a job is detached rather than pretending it is tracked', async () => {
    client.fetchJob.mockResolvedValue({
      running: true, name: 'seed', external: true, elapsed: 60, lines: [],
    })
    render(<RunningNow />)
    await waitFor(() =>
      expect(screen.getByText(/detached/)).toBeTruthy())
  })

  it('shows what the job is doing, not just how long it has run', async () => {
    client.fetchJob.mockResolvedValue({
      running: true, name: 'seed', external: true, elapsed: 1928,
      lines: ['[5/201] george@source.example: 4292 messages deleted'],
    })
    // Asserted against the rendered text rather than getByText: the detail
    // and the percentage are sibling text nodes inside one Typography, and
    // getByText matches a single node.
    const { container } = render(<RunningNow />)
    await waitFor(() =>
      expect(container.textContent).toMatch(/4292 messages deleted/))
  })

  it('reads elapsed time as a duration, not a second count', async () => {
    client.fetchJob.mockResolvedValue({
      running: true, name: 'seed', external: true, elapsed: 1928, lines: [],
    })
    render(<RunningNow />)
    await waitFor(() => expect(screen.getByText(/32m 08s/)).toBeTruthy())
  })

  it('ignores a job claimed by a node that stopped heartbeating', async () => {
    cp.fetchFleet.mockResolvedValue([
      node({ healthy: false, secondsSinceHeartbeat: 93416 }),
    ])
    render(<RunningNow />)
    await waitFor(() => expect(cp.fetchFleet).toHaveBeenCalled())
    expect(screen.queryByText('migrate')).toBeNull()
  })

  it('still shows a job on a node that is reporting', async () => {
    cp.fetchFleet.mockResolvedValue([node()])
    render(<RunningNow />)
    await waitFor(() => expect(screen.getByText('migrate')).toBeTruthy())
  })
})
