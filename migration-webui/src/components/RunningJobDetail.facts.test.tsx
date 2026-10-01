import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import RunningJobDetail from './RunningJobDetail'
import { jobFacts } from './RunningJobDetail.utils'
import type { RunningJob } from '@/hooks/useRunningJobs'

/* Every job without a dashboard of its own showed "Progress --, ETA --" even while its
   transcript counted every account it deleted. The window now reads what the job says. */

vi.mock('@/api/controlPlane', () => ({
  fetchTenantConfigStatus: vi.fn(), fetchFullSetupStatus: vi.fn(), fetchFleet: vi.fn(),
  fetchActiveJobs: vi.fn(), fetchMe: vi.fn(), stopJob: vi.fn(), fetchProvisionStatus: vi.fn(),
  fetchMyMetrics: vi.fn(),
}))
vi.mock('@/api/client', () => ({ fetchJob: vi.fn(), stopJob: vi.fn() }))

const DELETE = [
  'Sandbox guard passed for target2.example (target).',
  '299 user(s) would be deleted from target2.example (target)',
  '  [0/299] accounts deleted',
  '  [2/299] b@target2.example',
  '  [1/299] a@target2.example',
  '  ! c@target2.example: HttpError 500',
  '  [3/299] c@target2.example',
]

describe('jobFacts', () => {
  it('counts from the job, takes the largest of an out-of-order count, and measures the rate', () => {
    const f = jobFacts(DELETE, 120)
    expect([f.done, f.total]).toEqual([3, 299])
    expect(f.perMinute).toBeCloseTo(1.5)
    expect(f.etaSec).toBeCloseTo((296 / 1.5) * 60)
    expect(f.failures).toEqual(['  ! c@target2.example: HttpError 500'])
  })

  it('reads only the phase that is running now', () => {
    const f = jobFacts(['Mirror pass: Drive', '  [3/3] a', 'Mirror pass: mail and calendar', '  [1/3] a'], 60)
    expect([f.done, f.total]).toEqual([1, 3])
  })

  it('a job that counts nothing gets nothing, not a guess', () => {
    const f = jobFacts(['starting', 'still going'], 600)
    expect([f.done, f.total, f.perMinute, f.etaSec]).toEqual([null, null, null, null])
  })

  it('keeps the job\'s own result lines', () => {
    const f = jobFacts(['  [5/5] x', 'Removed: 3 drive, 2 gmail, 0 shared drive(s).', 'deleted 5, failed 0'], 60)
    expect(f.results).toEqual(['Removed: 3 drive, 2 gmail, 0 shared drive(s).', 'deleted 5, failed 0'])
  })
})

describe('the window', () => {
  const job = (o: Partial<RunningJob> = {}): RunningJob => ({
    key: 'k', kind: 'reset', label: 'wipe target', domain: 'target2.example',
    detail: 'deleting', pct: 1, elapsedSec: 120, lines: DELETE, ...o,
  })

  it('shows done of total, the rate, a measured ETA and the failures', () => {
    render(<RunningJobDetail job={job()} onClose={() => {}} />)
    expect(screen.getByText('3 of 299')).toBeInTheDocument()
    expect(screen.getByText('1.5 / min')).toBeInTheDocument()
    expect(screen.getByText('ETA (measured)')).toBeInTheDocument()
    expect(screen.getByTestId('job-failures')).toHaveTextContent('c@target2.example: HttpError 500')
  })

  it('shows the result a finished job printed', () => {
    render(<RunningJobDetail job={job({ done: true, rc: 0, lines: [...DELETE, 'deleted 298, failed 1'] })}
                             onClose={() => {}} />)
    expect(screen.getByTestId('job-results')).toHaveTextContent('deleted 298, failed 1')
  })
})
