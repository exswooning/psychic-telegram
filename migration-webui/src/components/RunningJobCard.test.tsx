/**
 * The sparkline is a trend on top of the number the card already shows --
 * it must never appear before there is a trend to show, and it must never
 * survive onto a card for a job that has already finished (there is
 * nothing left in flight for a trend to describe).
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import RunningJobCard from './RunningJobCard'
import type { RunningJob } from '@/hooks/useRunningJobs'

// controlPlane reads localStorage at module load, and useRunningJobs
// imports it -- this card only needs describeElapsed from that graph.
vi.mock('@/api/controlPlane', () => ({
  fetchReadiness: () => Promise.resolve({ side: "source", purpose: "migrate", domain: "", admin: "", ready: false, checks: [] }),
  fetchTenantConfigStatus: vi.fn(), fetchFullSetupStatus: vi.fn(),
  fetchFleet: vi.fn(), fetchActiveJobs: vi.fn(), fetchMe: vi.fn(),
  stopJob: vi.fn(), fetchProvisionStatus: vi.fn(),
}))
vi.mock('@/api/client', () => ({ fetchJob: vi.fn(), stopJob: vi.fn() }))

const job = (over: Partial<RunningJob> = {}): RunningJob => ({
  key: 'k1', label: 'seed', detail: 'seeding acme.com', pct: 10,
  kind: 'seed', ...over,
})

const hasChart = () => !!document.querySelector('.recharts-responsive-container')

describe('the progress sparkline', () => {
  it('does not appear on the first render -- one point is not a trend', () => {
    render(<RunningJobCard job={job({ pct: 10 })} />)
    expect(hasChart()).toBe(false)
  })

  it('appears once the same running job reports a second, different pct', () => {
    const { rerender: rr } = render(<RunningJobCard job={job({ key: 'k2', pct: 10 })} />)
    rr(<RunningJobCard job={job({ key: 'k2', pct: 40 })} />)
    expect(hasChart()).toBe(true)
  })

  it('does not appear on a finished job’s card', () => {
    render(<RunningJobCard job={job({ key: 'k3', pct: 40 })}
                          finished={{ rc: 0 }} />)
    expect(hasChart()).toBe(false)
  })

  it('a different job key starts its own fresh history', () => {
    const { rerender: rr } = render(<RunningJobCard job={job({ key: 'k4', pct: 10 })} />)
    rr(<RunningJobCard job={job({ key: 'k4', pct: 60 })} />)
    expect(hasChart()).toBe(true)
    // Switching to an unrelated job's single data point must not carry
    // the previous job's trend onto this one's card.
    rr(<RunningJobCard job={job({ key: 'k5', pct: 5 })} />)
    expect(hasChart()).toBe(false)
  })
})

describe('the job log', () => {
  const lines = ['[gcloud-auth] signed in as admin', '/x/site-packages/y.py: FutureWarning',
                 '[dwd] Authorize accepted. verifying...']

  it('shows what a running job printed, without import noise', () => {
    render(<RunningJobCard job={job({ lines })} />)
    const log = screen.getByTestId('job-log-k1')
    expect(log.textContent).toContain('[dwd] Authorize accepted')
    expect(log.textContent).not.toContain('FutureWarning')
    fireEvent.click(screen.getByTestId('job-log-toggle-k1'))
    expect(screen.queryByTestId('job-log-k1')).toBeNull()
  })

  it('starts at this run, not the runs before it in the same file', () => {
    render(<RunningJobCard job={job({ lines: ['old run line', '=== run started 2026-10-19T03:34:03Z: migrate ===',
                                              'this run line'] })} />)
    const text = screen.getByTestId('job-log-k1').textContent ?? ''
    expect(text).toContain('this run line')
    expect(text).not.toContain('old run line')
  })

  it('is not shown for a finished run', () => {
    render(<RunningJobCard job={job({ lines })} finished={{ rc: 0 }} />)
    expect(screen.queryByTestId('job-log-k1')).toBeNull()
  })
})
