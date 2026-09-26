/**
 * The verdict must not flatter. INCOMPLETE -- a check that could not be made -- is
 * amber, never green; and every real problem is named in the sentence, not only in
 * a colour.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import QuickVerification from './QuickVerification'

const api = vi.hoisted(() => ({ fetchQuickLatest: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchQuickLatest: api.fetchQuickLatest,
  quickReportUrl: (a?: number) => `/api/v2/quick/latest.md${a ? `?account_id=${a}` : ''}`,
}))

const svc = (over = {}) => ({ checked: 4, identical: 4, differences: [], missing: [], duplicates: [], extras: [], notCopied: [], errors: [], ...over })
const report = (over = {}) => ({
  generatedAt: '2026-09-26T10:00:00Z', verdict: 'IDENTICAL', sourceDomain: 'source.example', targetDomain: 'target2.example',
  sampleLimit: 20, services: ['drive', 'gmail'], reasons: [],
  totals: { checked: 8, identical: 8, differences: 0, missing: 0, duplicates: 0, extras: 0, notCopied: 0, errors: 0, filesOpened: 4, bytesCompared: 1234 },
  users: { 'george@source.example': { drive: svc(), gmail: svc() } }, ...over,
})

// Braces: a hook that RETURNS a function has it called afterwards as cleanup, and mockReset()
// returns the mock -- so this used to call the rejecting mock again after the test.
beforeEach(() => { api.fetchQuickLatest.mockReset() })

describe('QuickVerification', () => {
  it('says nothing when no quick migration has been checked', async () => {
    api.fetchQuickLatest.mockResolvedValue(null)
    const { container } = render(<QuickVerification accountId={3} />)
    await waitFor(() => expect(api.fetchQuickLatest).toHaveBeenCalledWith(3))
    expect(container).toBeEmptyDOMElement()
  })

  it('shows an identical result as green, with what was actually opened', async () => {
    api.fetchQuickLatest.mockResolvedValue(report())
    render(<QuickVerification accountId={3} />)
    const v = await screen.findByTestId('quick-verdict')
    expect(v).toHaveTextContent('IDENTICAL')
    expect(v.className).toMatch(/colorSuccess/)
    expect(screen.getByTestId('quick-summary')).toHaveTextContent('8 of 8')
    expect(screen.getByTestId('quick-summary')).toHaveTextContent('4 Drive files were downloaded from both sides')
  })

  it('shows INCOMPLETE as amber, never green, and says why', async () => {
    api.fetchQuickLatest.mockResolvedValue(report({
      verdict: 'INCOMPLETE', reasons: ['george@source.example: drive had 2 check(s) that could not be made'],
      totals: { ...report().totals, identical: 6, errors: 2 } }))
    render(<QuickVerification accountId={3} />)
    const v = await screen.findByTestId('quick-verdict')
    expect(v.className).toMatch(/colorWarning/)
    expect(v.className).not.toMatch(/colorSuccess/)
    expect(screen.getByTestId('quick-reasons')).toHaveTextContent('could not be made')
    expect(screen.getByTestId('quick-summary')).toHaveTextContent('2 check(s) could not be made')
  })

  it('shows differences as red and names each kind of problem in words', async () => {
    api.fetchQuickLatest.mockResolvedValue(report({
      verdict: 'DIFFERENCES',
      totals: { ...report().totals, identical: 3, differences: 2, missing: 1, duplicates: 1, notCopied: 1 },
      users: { 'george@source.example': { drive: svc({ identical: 1, differences: [{}, {}], missing: [{}], duplicates: [{}] }), gmail: svc() } } }))
    render(<QuickVerification accountId={3} />)
    expect((await screen.findByTestId('quick-verdict')).className).toMatch(/colorError/)
    const text = screen.getByTestId('quick-summary').textContent
    for (const s of ['2 differ', '1 missing on the target', '1 copied twice', '1 failed to copy']) expect(text).toContain(s)
    expect(screen.getByTestId('quick-cell-george-drive')).toHaveTextContent('1/4 · 4 problems')
    expect(screen.getByTestId('quick-cell-george-gmail')).toHaveTextContent('4/4')
  })

  it('says strays on the target are not this migration\'s, without calling them a failure', async () => {
    api.fetchQuickLatest.mockResolvedValue(report({ totals: { ...report().totals, extras: 3 } }))
    render(<QuickVerification accountId={3} />)
    expect(await screen.findByTestId('quick-summary')).toHaveTextContent('3 other item(s) on the target that this migration did not create')
    expect(screen.getByTestId('quick-verdict')).toHaveTextContent('IDENTICAL')
  })

  it('links the downloadable report for this account', async () => {
    api.fetchQuickLatest.mockResolvedValue(report())
    render(<QuickVerification accountId={3} />)
    expect(await screen.findByTestId('quick-download')).toHaveAttribute('href', '/api/v2/quick/latest.md?account_id=3')
  })

  it('reports a failure to load rather than showing nothing', async () => {
    api.fetchQuickLatest.mockRejectedValue(new Error('HTTP 500'))
    render(<QuickVerification accountId={3} />)
    expect(await screen.findByText('HTTP 500')).toBeInTheDocument()
  })

  it('reads again when a run starts or ends', async () => {
    api.fetchQuickLatest.mockResolvedValue(report())
    const { rerender } = render(<QuickVerification accountId={3} refreshKey={false} />)
    await screen.findByTestId('quick-verdict')
    rerender(<QuickVerification accountId={3} refreshKey={true} />)
    await waitFor(() => expect(api.fetchQuickLatest).toHaveBeenCalledTimes(2))
  })
})
