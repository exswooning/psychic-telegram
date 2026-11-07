import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import Services from './Services'
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 1, migration_id: 3 }),
  migrationOf: (m: { id: number; migration_id: number }) => m.migration_id ?? m.id,
}))
vi.mock('@/api/client', () => ({
  fetchActions: () => Promise.resolve({ groups_inventory: { label: 'Groups' } }),
  fetchToggles: () => Promise.resolve({ toggles: { services: {} } }),
  fetchSharedDrives: () => Promise.resolve(null), setToggles: vi.fn(),
}))
vi.mock('@/components/TenantScopePicker', () => ({ default: (p: { accountId: number; locked: boolean }) => <div data-testid="scope">{p.accountId}:{String(p.locked)}</div> }))
vi.mock('@/components/JobRunner', () => ({ default: (p: { accountId: number }) => <div data-testid="job">{p.accountId}</div> }))
vi.mock('@/components/ConfigurationReport', () => ({ default: (p: { accountId: number }) => <div data-testid="report">{p.accountId}</div> }))
vi.mock('@/components/DmsImportButton', () => ({ default: () => null }))
vi.mock('@/components/DmsMetrics', () => ({ default: () => null }))
describe('services uses the selected migration', () => {
  it('uses migration 3 everywhere rather than login 1 or a second domain selection', async () => {
    render(<Services />)
    expect(await screen.findByTestId('scope')).toHaveTextContent('3:true')
    expect(screen.getByTestId('report')).toHaveTextContent('3')
    expect(screen.getByTestId('job')).toHaveTextContent('3')
  })
})
