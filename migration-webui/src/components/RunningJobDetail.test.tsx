import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import RunningJobDetail, { projectedEta } from './RunningJobDetail'
import RunningJobCard from './RunningJobCard'
import type { RunningJob } from '@/hooks/useRunningJobs'

// controlPlane reads localStorage at module load, and the hook imports it.
// These components only need describeElapsed from that module graph.
vi.mock('@/api/controlPlane', () => ({
  fetchTenantConfigStatus: vi.fn(), fetchFullSetupStatus: vi.fn(),
  fetchFleet: vi.fn(), fetchActiveJobs: vi.fn(), fetchMe: vi.fn(),
  stopJob: vi.fn(), fetchProvisionStatus: vi.fn(),
}))
vi.mock('@/api/client', () => ({ fetchJob: vi.fn(), stopJob: vi.fn() }))

/* The card is a glance. Everything it cannot fit -- ETA, observed
   throughput, per-user detail -- used to live nowhere: a seed running for
   51 minutes showed a bar and a sentence, and there was no way to ask how
   much longer. */

const job = (o: Partial<RunningJob> = {}): RunningJob => ({
  key: 'k', kind: 'seed', label: 'seed', domain: 'src.test',
  detail: 'seeding 34/200 users', pct: 17, elapsedSec: 600, ...o,
})

describe('projected ETA', () => {
  it('projects from what has elapsed and what is done', () => {
    // 25% in 100s implies 300s left at the same rate
    expect(projectedEta(25, 100)).toBe(300)
  })

  it('refuses to project without a percentage', () => {
    expect(projectedEta(null, 600)).toBeNull()
  })

  it('refuses at 0%, where the rate is unknown rather than infinite', () => {
    expect(projectedEta(0, 600)).toBeNull()
  })

  it('refuses at 100%, where there is nothing left to project', () => {
    expect(projectedEta(100, 600)).toBeNull()
  })

  it('refuses without a clock', () => {
    expect(projectedEta(50, undefined)).toBeNull()
  })
})

describe('detail view', () => {
  it('shows elapsed, progress and an ETA', () => {
    render(<RunningJobDetail job={job()} onClose={() => {}} />)
    expect(screen.getByText('Elapsed')).toBeInTheDocument()
    expect(screen.getByText('10m 00s')).toBeInTheDocument()
    expect(screen.getByText('17%')).toBeInTheDocument()
    expect(screen.getByText(/ETA/)).toBeInTheDocument()
  })

  it('says why there is no ETA rather than showing a zero', () => {
    render(<RunningJobDetail job={job({ pct: null })} onClose={() => {}} />)
    expect(screen.getByText('needs a percentage')).toBeInTheDocument()
  })

  it('renders nothing when no job is selected', () => {
    const { container } = render(<RunningJobDetail job={null} onClose={() => {}} />)
    expect(container.textContent).toBe('')
  })
})

describe('card', () => {
  it('opens the detail view when clicked', () => {
    const onOpen = vi.fn()
    render(<RunningJobCard job={job()} onOpen={onOpen} />)
    fireEvent.click(screen.getByTestId('running-job-seed'))
    expect(onOpen).toHaveBeenCalled()
  })

  it('does not open it when the Stop button inside is clicked', () => {
    const onOpen = vi.fn()
    const stop = vi.fn()
    render(<RunningJobCard job={job()} onOpen={onOpen}
                           action={<button onClick={stop}>Stop</button>} />)
    fireEvent.click(screen.getByText('Stop'))
    expect(stop).toHaveBeenCalled()
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('names the job kind and the tenant', () => {
    render(<RunningJobCard job={job()} />)
    expect(screen.getByText('Seed')).toBeInTheDocument()
    expect(screen.getByText('src.test')).toBeInTheDocument()
    expect(screen.getByText('Running now')).toBeInTheDocument()
  })
})
