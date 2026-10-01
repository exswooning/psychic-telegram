/**
 * The reports panel is where a verdict is read, so what matters is that it
 * cannot flatter: UNVERIFIED is never shown as a pass, the one download points at
 * the right file, and the panel is there when nothing else is -- collapsed, but
 * still saying how many reports there are and the newest verdict.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import RunReports from './RunReports'

const api = vi.hoisted(() => ({ fetchReports: vi.fn(), generateReport: vi.fn(), startTally: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchReports: api.fetchReports, generateReport: api.generateReport, startTally: api.startTally,
  reportUrl: (id: string, what: string, account?: number | null) => {
    const a = account ? `account_id=${account}` : ''
    return what === 'json' ? `/api/v2/reports/${id}${a ? `?${a}` : ''}`
      : `/api/v2/reports/${id}/pdf?audience=${what}${a ? `&${a}` : ''}`
  },
}))

// The test runtime has no localStorage of its own; the card remembers open/closed in it.
const store: Record<string, string> = {}
vi.stubGlobal('localStorage', {
  getItem: (k: string) => store[k] ?? null,
  setItem: (k: string, v: string) => { store[k] = String(v) },
  removeItem: (k: string) => { delete store[k] },
})

const rep = (over = {}) => ({
  id: 'migration-20260925T195855Z', kind: 'migration', generatedAt: '2026-09-25T19:58:55Z',
  verdict: 'UNVERIFIED', counts: { pass: 6, warn: 1, fail: 0, unknown: 5 },
  tenants: { source: 'a.com', target: 'b.com' }, returnCode: null,
  startedAt: null, finishedAt: null, files: ['json', 'human.pdf', 'claude.pdf'], ...over,
})

beforeEach(() => {
  api.fetchReports.mockReset(); api.generateReport.mockReset(); api.startTally.mockReset()
  api.fetchReports.mockResolvedValue({ accountId: 1, reports: [], error: '' })
  localStorage.setItem('runReports.open', '1')   // most tests read the open card
})

describe('RunReports', () => {
  it('says plainly when there are none, and still offers to make one', async () => {
    render(<RunReports />)
    expect(await screen.findByTestId('no-reports')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate report now' })).toBeEnabled()
  })

  it('shows the verdict, what was checked, and the two tenants', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep()], error: '' })
    render(<RunReports />)
    const v = await screen.findByTestId('verdict-migration-20260925T195855Z')
    expect(v).toHaveTextContent('UNVERIFIED')
    const row = screen.getByTestId('report-migration-20260925T195855Z')
    expect(row).toHaveTextContent('6 passed · 1 warning · 0 failed · 5 not checked')
    expect(row).toHaveTextContent('a.com → b.com')
  })

  it('shows the per-user one-to-one rollup, not just the tenant-wide fidelity counts', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep({
      oneToOne: { IDENTICAL: 3, DIFFERENCES: 1, NOT_VERIFIED: 297 } })], error: '' })
    render(<RunReports />)
    expect(await screen.findByTestId('one-to-one-migration-20260925T195855Z'))
      .toHaveTextContent('One-to-one: 3 identical, 1 with differences, 297 not verified')
  })

  it('says nothing about one-to-one for a report that has none (a seed report)', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep({ kind: 'seed', oneToOne: null })], error: '' })
    render(<RunReports />)
    await screen.findByTestId('report-migration-20260925T195855Z')
    expect(screen.queryByTestId('one-to-one-migration-20260925T195855Z')).toBeNull()
  })

  it('never shows an unverified run as a pass', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep()], error: '' })
    render(<RunReports />)
    await screen.findByTestId('verdict-migration-20260925T195855Z')
    expect(screen.queryByText('PASS')).toBeNull()
    // Amber, not green.
    expect(screen.getByTestId('verdict-migration-20260925T195855Z').className).toMatch(/colorWarning/)
  })

  it('colours each verdict for what it means', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, error: '', reports: [
      rep({ id: 'migration-20260101T000000Z', verdict: 'PASS' }),
      rep({ id: 'migration-20260102T000000Z', verdict: 'FAIL' })] })
    render(<RunReports />)
    expect((await screen.findByTestId('verdict-migration-20260101T000000Z')).className).toMatch(/colorSuccess/)
    expect(screen.getByTestId('verdict-migration-20260102T000000Z').className).toMatch(/colorError/)
  })

  it('offers one download per report, the readable PDF, as a download', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep()], error: '' })
    render(<RunReports />)
    const dl = await screen.findByTestId('download-migration-20260925T195855Z')
    expect(dl).toHaveTextContent('Download report')
    expect(dl).toHaveAttribute('href', '/api/v2/reports/migration-20260925T195855Z/pdf?audience=human')
    expect(dl).toHaveAttribute('download')
    expect(screen.queryByText('Claude PDF')).toBeNull()
    expect(screen.queryByText('JSON')).toBeNull()
  })

  it('falls back to the other PDF when a report only has that one', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep({ files: ['json', 'claude.pdf'] })], error: '' })
    render(<RunReports />)
    expect(await screen.findByTestId('download-migration-20260925T195855Z'))
      .toHaveAttribute('href', '/api/v2/reports/migration-20260925T195855Z/pdf?audience=claude')
  })

  it('disables a download whose file does not exist rather than offering a dead link', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep({ files: ['json'] })], error: '' })
    render(<RunReports />)
    expect(await screen.findByTestId('download-migration-20260925T195855Z')).toHaveAttribute('aria-disabled', 'true')
  })

  it('starts collapsed, still saying how many reports and the newest verdict, and opens', async () => {
    localStorage.removeItem('runReports.open')
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep(), rep({ id: 'older', verdict: 'FAIL' })], error: '' })
    render(<RunReports />)
    const summary = await screen.findByTestId('reports-summary')
    await waitFor(() => expect(summary).toHaveTextContent('2 reports'))
    expect(summary).toHaveTextContent('latest UNVERIFIED')
    expect(screen.queryByTestId('report-migration-20260925T195855Z')).toBeNull()
    fireEvent.click(screen.getByTestId('reports-toggle'))
    expect(await screen.findByTestId('report-migration-20260925T195855Z')).toBeInTheDocument()
    expect(localStorage.getItem('runReports.open')).toBe('1')
  })

  it('generates a report and shows it', async () => {
    api.generateReport.mockResolvedValue(rep())
    render(<RunReports />)
    await screen.findByTestId('no-reports')
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep()], error: '' })
    fireEvent.click(screen.getByRole('button', { name: 'Generate report now' }))
    await waitFor(() => expect(api.generateReport).toHaveBeenCalled())
    expect(await screen.findByTestId('report-migration-20260925T195855Z')).toBeInTheDocument()
  })

  it('says what went wrong when generating fails, and can be tried again', async () => {
    api.generateReport.mockRejectedValue(new Error('this account has no migration ledger yet'))
    render(<RunReports />)
    await screen.findByTestId('no-reports')
    fireEvent.click(screen.getByRole('button', { name: 'Generate report now' }))
    expect(await screen.findByText('this account has no migration ledger yet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate report now' })).toBeEnabled()
  })

  it('still offers generation when the list itself cannot be read', async () => {
    api.fetchReports.mockRejectedValue(new Error('HTTP 500'))
    render(<RunReports />)
    expect(await screen.findByText('HTTP 500')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate report now' })).toBeEnabled()
  })

  describe('for one account, or across accounts', () => {
    it('asks for and generates for the account it was given', async () => {
      api.generateReport.mockResolvedValue(rep())
      render(<RunReports accountId={7} />)
      await screen.findByTestId('no-reports')
      expect(api.fetchReports).toHaveBeenCalledWith(7)
      fireEvent.click(screen.getByRole('button', { name: 'Generate report now' }))
      await waitFor(() => expect(api.generateReport).toHaveBeenCalledWith(7))
    })

    it('labels each report with its account when looking across accounts, and links to that account\'s file', async () => {
      api.fetchReports.mockResolvedValue({ accountId: 1, scope: 'all', error: '',
                                            reports: [rep({ accountId: 2 })] })
      render(<RunReports />)
      const row = await screen.findByTestId('report-migration-20260925T195855Z')
      expect(row).toHaveTextContent('account #2')
      expect(screen.getByTestId('download-migration-20260925T195855Z'))
        .toHaveAttribute('href', '/api/v2/reports/migration-20260925T195855Z/pdf?audience=human&account_id=2')
    })

    it('does not repeat the account on that account\'s own page', async () => {
      api.fetchReports.mockResolvedValue({ accountId: 2, error: '', reports: [rep({ accountId: 2 })] })
      render(<RunReports accountId={2} />)
      expect(await screen.findByTestId('report-migration-20260925T195855Z')).not.toHaveTextContent('account #')
    })
  })

  it('starts a tally for the account on screen and says what happened', async () => {
    api.startTally.mockResolvedValue({ ok: true, actionId: 1, detail: 'the box is busy -- queued at position 1' })
    render(<RunReports accountId={68} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Run tally' }))
    await waitFor(() => expect(api.startTally).toHaveBeenCalledWith(68))
    expect(await screen.findByTestId('tally-started')).toHaveTextContent('queued at position 1')
  })

  it('shows why a tally could not start', async () => {
    api.startTally.mockRejectedValue(new Error('that migration belongs to another account'))
    render(<RunReports />)
    fireEvent.click(await screen.findByRole('button', { name: 'Run tally' }))
    expect(await screen.findByText('that migration belongs to another account')).toBeInTheDocument()
    expect(screen.queryByTestId('tally-started')).toBeNull()
  })
})
