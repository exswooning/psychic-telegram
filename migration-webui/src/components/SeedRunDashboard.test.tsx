import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import SeedRunDashboard from './SeedRunDashboard'

/**
 * These pin the two honesty rules, not the layout. Both exist because the
 * opposite behaviour shipped and was wrong in a way that looked right:
 *
 *   * a figure the run never printed rendering as 0 is indistinguishable
 *     from a real measured zero (a seed genuinely produces 0 contacts when
 *     the People API is off), so "not reported" has to render as "--";
 *   * an ETA extrapolated from too small a sample displayed a confident
 *     300h against the run's own 11h44m estimate -- wrong by 25x, in
 *     accent colour -- because 1 of 201 users had finished across 9
 *     parallel workers.
 *
 * Layout can change freely; these must not.
 */
const banner = (over: Partial<{ users: number; workers: number }> = {}) => [
  `Seeding ${over.users ?? 201} users in source.example.com at scale 'huge'`,
  `Workers: ${over.workers ?? 9} (memory-bound: 3.0 GB usable / 320 MB per worker = 9)`,
  '  estimated ~2,661,240 API writes, roughly 704 minute(s) at 9 parallel users',
]

const doneUser = (n: number) =>
  `  [u${n}@source.example.com] done in 100s: 10 files, 2 folders`

/** Addressed by test id: several stat labels ("files", "contacts", ...) are
 *  also table column headers, so a plain text lookup is ambiguous. */
const statValue = (label: string): string =>
  screen.getByTestId(`stat-${label}`).textContent!.replace(label, '').trim()

describe('withholding an ETA until the sample supports one', () => {
  it('shows no observed rate or ETA before one full worker batch finishes', () => {
    render(<SeedRunDashboard lines={[...banner(), doneUser(1)]} elapsedSec={5400} />)
    // 1 of 201 done across 9 parallel workers: extrapolating here is what
    // produced the 300h figure.
    expect(statValue('ETA (observed)')).toBe('--')
    expect(statValue('Observed rate')).toBe('--')
  })

  it('shows them once a full batch has landed', () => {
    const lines = [...banner(), ...Array.from({ length: 9 }, (_, i) => doneUser(i))]
    render(<SeedRunDashboard lines={lines} elapsedSec={5400} />)
    expect(statValue('ETA (observed)')).not.toBe('--')
    expect(statValue('Observed rate')).not.toBe('--')
  })

  it("still shows the run's own up-front estimate meanwhile", () => {
    // Withholding the derived figure must not hide the measured one.
    render(<SeedRunDashboard lines={[...banner(), doneUser(1)]} elapsedSec={5400} />)
    expect(statValue('Est. at start')).toBe('11h 44m')
  })

  it('needs at least two users even when the run is single-worker', () => {
    render(<SeedRunDashboard lines={[...banner({ workers: 1 }), doneUser(1)]} elapsedSec={600} />)
    expect(statValue('ETA (observed)')).toBe('--')
  })
})

describe('never reporting an unmeasured value as zero', () => {
  it('renders "--" for a count the run did not print for that user', () => {
    const lines = [
      ...banner(),
      '  [a@source.example.com] done in 100s: 10 files, 2 folders, 0 contacts',
      '  [b@source.example.com] done in 100s: 5 files, 1 folders',
    ]
    render(<SeedRunDashboard lines={lines} elapsedSec={200} />)

    const rowB = screen.getByText('b@source.example.com').closest('tr')!
    // a@ reported a real 0 for contacts; b@ never reported contacts at all.
    // They must not look the same.
    const rowA = screen.getByText('a@source.example.com').closest('tr')!
    expect(within(rowA).getByText('0')).toBeInTheDocument()
    expect(within(rowB).getAllByText('--').length).toBeGreaterThan(0)
  })

  it('shows an in-flight user with no counts at all rather than zeroes', () => {
    const lines = [...banner(), '  [c@source.example.com] starting (Sales, PRJ-003)']
    render(<SeedRunDashboard lines={lines} elapsedSec={60} />)
    const row = screen.getByText('c@source.example.com').closest('tr')!
    expect(within(row).getByText('in flight')).toBeInTheDocument()
    expect(within(row).queryByText('0')).toBeNull()
  })
})

describe('surfacing what the run measured', () => {
  it('totals only finished users, and labels how many that was', () => {
    const lines = [
      ...banner(),
      '  [a@source.example.com] done in 100s: 10 files',
      '  [b@source.example.com] done in 100s: 15 files',
      '  [c@source.example.com] starting (Sales, PRJ-003)',
    ]
    render(<SeedRunDashboard lines={lines} elapsedSec={200} />)
    expect(screen.getByText(/summed across 2 finished users/)).toBeInTheDocument()
    expect(statValue('files')).toBe('25')
  })

  it('groups repeated warnings with a count instead of listing each', () => {
    const lines = [
      ...banner(),
      '  ! label Clients: HTTP 409 (aborted): <HttpError 409 ...>',
      '  ! label Acme: HTTP 409 (aborted): <HttpError 409 ...>',
      '  ! chat for a@x.com: HTTP 404 (NOT_FOUND): <HttpError 404 ...>',
    ]
    render(<SeedRunDashboard lines={lines} elapsedSec={60} />)
    expect(screen.getByText(/3 total, 2 distinct/)).toBeInTheDocument()
    expect(screen.getByText('2x')).toBeInTheDocument()
  })

  it('flags services that failed inside an otherwise finished user', () => {
    const lines = [...banner(),
      '  [a@source.example.com] done in 100s: 10 files, 0 contacts '
      + '(contacts failed (People API enabled?): HTTP 409 ...)']
    render(<SeedRunDashboard lines={lines} elapsedSec={100} />)
    const row = screen.getByText('a@source.example.com').closest('tr')!
    expect(within(row).getByText('contacts')).toBeInTheDocument()
  })

  it('renders nothing at all when there is no seed run in the output', () => {
    const { container } = render(<SeedRunDashboard lines={['some unrelated log line']} />)
    expect(container).toBeEmptyDOMElement()
  })
})
