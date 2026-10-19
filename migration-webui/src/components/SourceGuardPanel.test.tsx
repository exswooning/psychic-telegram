import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import SourceGuardPanel from './SourceGuardPanel'

const g = vi.hoisted(() => ({ value: null as unknown }))
vi.mock('@/api/controlPlane', () => ({
  fetchSourceGuard: () => Promise.resolve(g.value),
  runSourceGuard: vi.fn(),
}))

describe('the source guard', () => {
  it('says the source changed, what changed, and what Google says this tool did', async () => {
    g.value = { snapshotTakenAt: '2026-10-19T06:24:00Z', snapshotUsers: 45, latest: {
      checkedAt: '2026-10-19T14:00:00Z', since: '2026-10-19T13:50:00Z', verdict: 'CHANGED', changeCount: 1,
      changes: [{ user: 'info@a.gov.np', what: 'file created', name: 'Copy of Survey', by: "Google itself, on info@a.gov.np's behalf (copy, no app, no address)" }], unread: [],
      auditLog: { byBitport: [{ at: '2026-10-19T05:47:46Z', app: 'drive', event: 'copy', actor: 'info@a.gov.np', item: 'Copy of Survey' }],
                  deletionsByOthers: [] } } }
    render(<SourceGuardPanel accountId={5} sourceDomain="a.gov.np" />)
    expect(await screen.findByTestId('source-guard-verdict')).toHaveTextContent('Changed')
    expect(screen.getByText('Bitport changed 1')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('source-guard-details'))
    expect(await screen.findByText(/info@a.gov.np · file created · Copy of Survey — by Google itself/)).toBeInTheDocument()
  })

  it('never calls an unread source unchanged', async () => {
    g.value = { snapshotTakenAt: '2026-10-19T06:24:00Z', snapshotUsers: 45, latest: {
      checkedAt: 'x', since: 'y', verdict: 'INCOMPLETE', changeCount: 0, changes: [],
      unread: [{ user: 'a@a.gov.np', errors: { drive: '403' } }], auditLog: { error: 'scope refused' } } }
    render(<SourceGuardPanel accountId={5} />)
    expect(await screen.findByTestId('source-guard-verdict')).toHaveTextContent('Could not check all of it')
  })

  it('says there is no baseline rather than passing', async () => {
    g.value = { snapshotTakenAt: null, snapshotUsers: 0, latest: null }
    render(<SourceGuardPanel accountId={5} />)
    expect(await screen.findByTestId('source-guard-verdict')).toHaveTextContent('No baseline')
  })
})
