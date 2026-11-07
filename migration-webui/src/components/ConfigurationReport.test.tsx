import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ConfigurationReport from './ConfigurationReport'
import type { ConfigurationReport as Report } from '@/api/client'

const load = vi.hoisted(() => vi.fn())
vi.mock('@/api/client', () => ({ fetchConfigurationReport: (...a: unknown[]) => load(...a) }))
vi.mock('./JobRunner', () => ({ default: (p: { name: string; disabled?: boolean; accountId: number; extra?: object }) =>
  <button disabled={p.disabled} data-testid={p.name} data-account={p.accountId} data-extra={JSON.stringify(p.extra)}>{p.name}</button> }))
const actions = {
  configuration_check: { label: 'Inspect', blurb: '', destructive: false, confirm: '' },
  configuration_apply_groups: { label: 'Apply', blurb: '', destructive: true, confirm: 'GROUPS' },
}
const report = (): Report => ({
  sourceDomain: 'source.test', targetDomain: 'target.test', checkedAt: '2026-10-29T00:00:00Z', planHash: 'a'.repeat(64),
  groups: [{ source: 'team@source.test', target: 'team@target.test', exists: true, status: 'different',
    differences: [{ field: 'whoCanPostMessage', source: 'ALL_MEMBERS_CAN_POST', target: 'ALL_MANAGERS_CAN_POST' }],
    blocked: [], operations: [{ kind: 'settings' }] }],
  groupInventory: { source: { status: 'read', value: [] }, target: { status: 'read', value: [] } },
  storage: [{ source: 'info@source.test', target: 'nest@target.test',
    sourceQuota: { status: 'read', checkedAt: 'source read', value: { usageInDrive: '90071992547409931', limit: null } },
    targetQuota: { status: 'unreadable', checkedAt: 'target read', error: 'Not authorized' }, targetMinusSourceBytes: {} }],
  storagePolicy: { status: 'manual_required', reason: 'Quota is not policy', items: ['Group priority'] }, limits: ['No group archive import'],
})
beforeEach(() => { load.mockReset(); load.mockResolvedValue({ report: report() }) })
describe('configuration evidence in the UI', () => {
  it('shows exact bytes, missing access and policy limitations', async () => {
    render(<ConfigurationReport accountId={3} actions={actions} />)
    expect(await screen.findByText('90071992547409931 bytes')).toBeVisible()
    expect(screen.getByText('Target not checked: Not authorized')).toBeVisible()
    expect(screen.getByText('Storage policy: Admin Console review required')).toBeVisible()
    expect(load).toHaveBeenCalledWith(3)
    expect(screen.getByTestId('configuration_apply_groups')).toBeDisabled()
  })
  it('requires explicit review and sends the displayed plan hash and tenant', async () => {
    render(<ConfigurationReport accountId={3} actions={actions} />)
    const review = await screen.findByRole('checkbox')
    fireEvent.click(screen.getByRole('button', { name: /team@source.test/ }))
    expect(screen.getByRole('table', { name: 'Group differences team@source.test' })).toHaveTextContent('All members can post')
    fireEvent.click(review)
    const apply = screen.getByTestId('configuration_apply_groups')
    expect(apply).toBeEnabled()
    expect(apply).toHaveAttribute('data-account', '3')
    expect(apply).toHaveAttribute('data-extra', JSON.stringify({ review_hash: 'a'.repeat(64) }))
  })
  it('blocks apply for unreadable settings even after review', async () => {
    const r = report(); r.groups[0].blocked = ['Settings unavailable']; r.groups[0].status = 'not_checked'
    load.mockResolvedValue({ report: r })
    render(<ConfigurationReport accountId={3} actions={actions} />)
    expect(await screen.findByRole('checkbox')).toBeDisabled()
    expect(screen.getByTestId('configuration_apply_groups')).toBeDisabled()
    expect(screen.getByText('Checks incomplete')).toBeVisible()
  })
  it('does not turn a failed read into an empty matched report', async () => {
    load.mockRejectedValue(new Error('HTTP 503'))
    render(<ConfigurationReport accountId={3} actions={actions} />)
    expect(await screen.findByText(/Configuration evidence unavailable/)).toBeVisible()
    expect(screen.queryByText('Compared fields match')).toBeNull()
  })
  it('explains when the backend checker is absent', async () => {
    load.mockResolvedValue({ report: null })
    render(<ConfigurationReport accountId={3} actions={{}} />)
    expect(screen.getByText(/not installed on this server/)).toBeVisible()
    expect(await screen.findByText(/Not checked yet/)).toBeVisible()
  })
  it('clears old tenant evidence after a selection changes', async () => {
    const { rerender } = render(<ConfigurationReport accountId={3} actions={actions} />)
    await screen.findByText('90071992547409931 bytes')
    load.mockResolvedValue({ report: null })
    rerender(<ConfigurationReport accountId={4} actions={actions} />)
    await waitFor(() => expect(load).toHaveBeenLastCalledWith(4))
    expect(screen.queryByText('90071992547409931 bytes')).toBeNull()
  })
})
