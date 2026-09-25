/**
 * The reports panel is where a verdict is read, so what matters is that it
 * cannot flatter: UNVERIFIED is never shown as a pass, the downloads point at
 * the right files, and the panel is there when nothing else is.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import RunReports from './RunReports'

const api = vi.hoisted(() => ({ fetchReports: vi.fn(), generateReport: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchReports: api.fetchReports, generateReport: api.generateReport,
  reportUrl: (id: string, what: string) =>
    what === 'json' ? `/api/v2/reports/${id}` : `/api/v2/reports/${id}/pdf?audience=${what}`,
}))

const rep = (over = {}) => ({
  id: 'migration-20260925T195855Z', kind: 'migration', generatedAt: '2026-09-25T19:58:55Z',
  verdict: 'UNVERIFIED', counts: { pass: 6, warn: 1, fail: 0, unknown: 5 },
  tenants: { source: 'a.com', target: 'b.com' }, returnCode: null,
  startedAt: null, finishedAt: null, files: ['json', 'human.pdf', 'claude.pdf'], ...over,
})

beforeEach(() => {
  api.fetchReports.mockReset(); api.generateReport.mockReset()
  api.fetchReports.mockResolvedValue({ accountId: 1, reports: [], error: '' })
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

  it('links each download to the right file, as a download', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep()], error: '' })
    render(<RunReports />)
    const human = await screen.findByTestId('pdf-human-migration-20260925T195855Z')
    const claude = screen.getByTestId('pdf-claude-migration-20260925T195855Z')
    expect(human).toHaveAttribute('href', '/api/v2/reports/migration-20260925T195855Z/pdf?audience=human')
    expect(claude).toHaveAttribute('href', '/api/v2/reports/migration-20260925T195855Z/pdf?audience=claude')
    expect(human).toHaveAttribute('download')
  })

  it('disables a download whose file does not exist rather than offering a dead link', async () => {
    api.fetchReports.mockResolvedValue({ accountId: 1, reports: [rep({ files: ['json'] })], error: '' })
    render(<RunReports />)
    expect(await screen.findByTestId('pdf-human-migration-20260925T195855Z')).toHaveAttribute('aria-disabled', 'true')
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
})
