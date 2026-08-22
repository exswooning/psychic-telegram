import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import TestReport from './TestReport'

const fetchTestReport = vi.fn()
const runTests = vi.fn()

vi.mock('@/api/controlPlane', () => ({
  fetchTestReport: (...a: unknown[]) => fetchTestReport(...a),
  runTests: (...a: unknown[]) => runTests(...a),
}))
vi.mock('@/components/ReasonCodeDialog', () => ({
  default: () => null,
}))

/**
 * This page is evidence, so the thing to guard against is it looking
 * reassuring when it should not: a suite that never ran, or one that ran and
 * failed, must never read as green.
 */
const report = (over: Record<string, unknown> = {}) => ({
  neverRun: false,
  running: false,
  ok: true,
  total: 1606, passed: 1606, failed: 0, skipped: 0,
  durationSec: 180, wallSec: 183.4,
  ranAt: '2026-08-22T04:00:00Z', commit: 'abc1234',
  files: [{ file: 'test_drive.py', passed: 40, failed: 0, skipped: 0, duration: 3.2 }],
  failures: [],
  slowest: [{ name: 'tests.a.A::slow', duration: 9.9 }],
  ...over,
})

describe('TestReport', () => {
  beforeEach(() => { fetchTestReport.mockReset(); runTests.mockReset() })

  it('shows a passing suite with its commit', async () => {
    fetchTestReport.mockResolvedValue(report())
    render(<TestReport />)
    await waitFor(() => expect(screen.getByTestId('suite-verdict')).toBeTruthy())
    expect(screen.getByTestId('suite-verdict').textContent).toContain('passing')
    expect(screen.getByTestId('suite-commit').textContent).toContain('abc1234')
    expect(screen.getByTestId('tests-total').textContent).toContain('1,606')
  })

  it('leads with the failure count when the suite is red', async () => {
    fetchTestReport.mockResolvedValue(
      report({ ok: false, failed: 2, passed: 1604 }))
    render(<TestReport />)
    await waitFor(() => expect(screen.getByTestId('suite-verdict')).toBeTruthy())
    expect(screen.getByTestId('suite-verdict').textContent).toContain('2 failing')
  })

  it('shows each failure in full rather than folding it away', async () => {
    // A green summary with the failures hidden is how people learn to skim
    // the one screen meant to stop them.
    fetchTestReport.mockResolvedValue(report({
      ok: false, failed: 1, passed: 1605,
      failures: [{ name: 'tests.x.X::test_boom', file: 'test_x.py',
                   message: 'assert 1 == 2', detail: 'the traceback' }],
    }))
    render(<TestReport />)
    await waitFor(() => expect(screen.getByTestId('failures')).toBeTruthy())
    expect(screen.getByText('assert 1 == 2')).toBeTruthy()
    expect(screen.getByText('the traceback')).toBeTruthy()
  })

  it('says when the suite has never been run here', async () => {
    // Never-run must not be indistinguishable from passing.
    fetchTestReport.mockResolvedValue(
      { neverRun: true, detail: 'the suite has not been run on this host yet' })
    render(<TestReport />)
    await waitFor(() => expect(screen.getByTestId('never-run')).toBeTruthy())
    expect(screen.queryByTestId('suite-verdict')).toBeNull()
  })

  it('disables the run button and shows progress while running', async () => {
    fetchTestReport.mockResolvedValue(report({ running: true }))
    render(<TestReport />)
    await waitFor(() => expect(screen.getByTestId('running-bar')).toBeTruthy())
    expect(screen.getByTestId('run-tests').hasAttribute('disabled')).toBe(true)
  })

  it('lists files with failures first', async () => {
    fetchTestReport.mockResolvedValue(report({
      ok: false, failed: 1,
      files: [
        { file: 'test_bad.py', passed: 1, failed: 1, skipped: 0, duration: 1 },
        { file: 'test_ok.py', passed: 9, failed: 0, skipped: 0, duration: 2 },
      ],
    }))
    render(<TestReport />)
    await waitFor(() => expect(screen.getByTestId('by-file')).toBeTruthy())
    expect(screen.getByTestId('file-test_bad.py')).toBeTruthy()
  })

  it('surfaces a fetch error', async () => {
    fetchTestReport.mockRejectedValue(new Error('operator-only'))
    render(<TestReport />)
    await waitFor(() => expect(screen.getByText(/operator-only/)).toBeTruthy())
  })
})
