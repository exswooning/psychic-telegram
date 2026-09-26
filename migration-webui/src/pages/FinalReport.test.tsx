/**
 * The Final Report tab. Two promises: the saved reports are here whatever the
 * live summary says (the tab is where you go to ask "how did it go", and that
 * answer must not depend on a job being alive), and nothing on it pretends.
 * It used to carry five buttons with no handler -- "Download PDF Report",
 * "Start Delta Sync" among them -- and a "Verification Success Rate" that was
 * only the share of users not marked FAILED.
 */
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import FinalReport from './FinalReport'
import { useMigrationStore } from '@/store'

vi.mock('@/components/RunReports', () => ({ default: () => <div data-testid="run-reports" /> }))
vi.mock('@/components/Incidents', () => ({ default: () => <div data-testid="incidents" /> }))

const report = (over = {}) => ({
  totalUsers: 10, successfulUsers: 8, failedUsers: 2, dataMigrated: '1.2 GB', emailsMigrated: 1,
  driveFilesMigrated: 2, calendarEvents: 3, contacts: 4, groups: 5, sharedDrives: 6,
  totalDuration: '1h 0m', averageThroughput: '10 items/min', averageSpeed: '1 MB/s',
  verificationSuccessRate: 80, ...over,
})

beforeEach(() => useMigrationStore.setState({ report: null }))

describe('Final Report tab', () => {
  it('shows the saved reports even when nothing has run', () => {
    render(<FinalReport />)
    expect(screen.getByTestId('run-reports')).toBeInTheDocument()
  })

  it('shows them alongside a live summary too', () => {
    useMigrationStore.setState({ report: report() as never })
    render(<FinalReport />)
    expect(screen.getByTestId('run-reports')).toBeInTheDocument()
    expect(screen.getByText('Total Users')).toBeInTheDocument()
  })

  it('shows a report whose ledger was reset as "nothing yet", not a green success', () => {
    useMigrationStore.setState({ report: report({ totalUsers: 0, successfulUsers: 0, failedUsers: 0 }) as never })
    render(<FinalReport />)
    expect(screen.getByText(/No live summary yet/)).toBeInTheDocument()
    expect(screen.queryByText('Migration Complete')).toBeNull()
  })

  it('has no button that does nothing', () => {
    useMigrationStore.setState({ report: report() as never })
    render(<FinalReport />)
    for (const dead of ['Download PDF Report', 'Download CSV', 'View Detailed Report', 'Export Logs', 'Start Delta Sync'])
      expect(screen.queryByRole('button', { name: dead })).toBeNull()
  })

  it('does not call the share of users not marked failed a verification', () => {
    useMigrationStore.setState({ report: report() as never })
    render(<FinalReport />)
    expect(screen.queryByText('Verification Success Rate')).toBeNull()
    expect(screen.getByTestId('users-clean')).toHaveTextContent('8 of 10')
    expect(screen.getByTestId('users-clean').parentElement?.textContent).not.toMatch(/%/)
  })

  it('keeps incidents on the page whether or not a migration has run', () => {
    // The watcher records problems on a tenant that has never finished a run.
    useMigrationStore.setState({ report: null } as never)
    const { unmount } = render(<FinalReport />)
    expect(screen.getByTestId('incidents')).toBeInTheDocument()
    unmount()
    useMigrationStore.setState({ report: report() } as never)
    render(<FinalReport />)
    expect(screen.getByTestId('incidents')).toBeInTheDocument()
  })
})
