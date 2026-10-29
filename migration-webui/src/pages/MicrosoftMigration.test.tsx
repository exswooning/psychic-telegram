import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { microsoftDiscover, microsoftMap, microsoftPilot, microsoftWorkspace, MicrosoftWorkspace } from '@/api/microsoft'
import MicrosoftMigration from './MicrosoftMigration'

vi.mock('@/api/microsoft', () => ({ microsoftWorkspace: vi.fn(), microsoftDiscover: vi.fn(), microsoftMap: vi.fn(), microsoftSave: vi.fn(), microsoftStop: vi.fn(), microsoftRecover: vi.fn(), microsoftPilot: vi.fn(), microsoftExport: vi.fn() }))

const fixture = (): MicrosoftWorkspace => ({
  accountId: 7,
  config: { source: { tenantId: 'source-id', clientId: 'app', cloud: 'commercial', region: '' }, target: { tenantId: 'target-id', clientId: 'app', cloud: 'commercial', region: 'Europe' }, mode: 'preserve_source', sandbox: false },
  credentials: { source: { ready: false, detail: 'Source certificate needed' }, target: { ready: false, detail: 'Target certificate needed' } },
  runs: [], receipts: [], repairs: [], sandboxWritesEnabled: false,
  plan: { inventoryId: null, inventoryCurrent: false, productionReady: false, locationVerified: false, planHash: 'reviewed-hash', summary: '', findings: [], targetUsers: [], identities: [], cutover: { ready: false, blockers: [] }, workloads: [{
    id: 'onedrive', label: 'OneDrive files and structure', proposedMethod: 'Native move / sandbox content copy', discoveryImplemented: true, transferQualified: false, sourceChanges: true,
    documentation: '', note: 'Versions and sharing are not preserved in the content pilot', readPermissions: ['Files.Read.All'], blockers: ['Fresh discovery required'], verification: 'NOT_RUN',
    inventory: { source: { state: 'NOT_CHECKED', records: 0, scopes: 0, incompleteScopes: 0 }, target: { state: 'NOT_CHECKED', records: 0, scopes: 0, incompleteScopes: 0 } },
  }] },
})

describe('Microsoft migration workspace', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(microsoftWorkspace).mockResolvedValue(fixture()) })
  it('shows missing connections without starting discovery', async () => {
    render(<MicrosoftMigration />)
    expect(await screen.findByText('Source certificate needed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Discover both tenants' })).toBeDisabled()
    expect(microsoftDiscover).not.toHaveBeenCalled()
    expect(screen.getByText(/Production migration, native moves and domain cutover remain unavailable/)).toBeInTheDocument()
  })
  it('does not present unread OneDrive as zero verified files', async () => {
    render(<MicrosoftMigration />)
    await screen.findByText('Source certificate needed')
    fireEvent.click(screen.getByRole('tab', { name: '2. Coverage & methods' }))
    expect(screen.getByText(/Inventory is not verification/)).toBeInTheDocument()
    expect(screen.getAllByText('not checked').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: /OneDrive files and structure/ }))
    expect(screen.getByText(/Verification: not run/)).toBeInTheDocument()
    expect(screen.queryByText('1:1 complete')).not.toBeInTheDocument()
  })
  it('loads failures visibly instead of fabricating a workspace', async () => {
    vi.mocked(microsoftWorkspace).mockRejectedValue(new Error('Sign in required'))
    render(<MicrosoftMigration />)
    expect(await screen.findByText('Sign in required')).toBeInTheDocument()
    expect(screen.queryByText('0 inventory records')).not.toBeInTheDocument()
  })
  it('shows people as unmapped without auto-confirming a suggestion', async () => {
    const value = fixture()
    value.plan.inventoryCurrent = true
    value.plan.targetUsers = [{ id: 'target-user', email: 'info@target.example', name: 'Info User' }]
    value.plan.identities = [{ sourceId: 'source-user', sourceName: 'दीप Shahi', sourceEmail: 'info@source.example', userType: 'Member', targetId: null, targetEmail: null, confirmed: false, suggestedTargetId: 'target-user', differences: [], profileState: 'NOT_COMPARED' }]
    vi.mocked(microsoftWorkspace).mockResolvedValue(value)
    render(<MicrosoftMigration />)
    await screen.findByText('Source certificate needed')
    fireEvent.click(screen.getByRole('tab', { name: '3. People' }))
    expect(screen.getByText('दीप Shahi')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm mapping' })).toBeDisabled()
    expect(microsoftMap).not.toHaveBeenCalled()
    expect(microsoftPilot).not.toHaveBeenCalled()
  })
  it('requires typed approval before sandbox profile writes', async () => {
    const value = fixture(); value.config.sandbox = true; value.sandboxWritesEnabled = true
    value.plan.targetUsers = [{ id: 'target-user', email: 'info@target.example', name: 'Target User' }]
    value.plan.identities = [{ sourceId: 'source-user', sourceName: 'दीप Shahi', sourceEmail: 'info@source.example', userType: 'Member', targetId: 'target-user', targetEmail: 'info@target.example', confirmed: true, suggestedTargetId: null, differences: [], profileState: 'MATCHED_SNAPSHOT' }]
    vi.mocked(microsoftWorkspace).mockResolvedValue(value)
    vi.mocked(microsoftPilot).mockResolvedValue({ ok: true })
    render(<MicrosoftMigration />)
    await screen.findByText('Source certificate needed')
    fireEvent.click(screen.getByRole('tab', { name: '3. People' }))
    fireEvent.click(screen.getByRole('button', { name: 'Copy profile in sandbox' }))
    expect(screen.getByRole('button', { name: 'Start sandbox copy' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /disposable sandbox/ }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Type COPY PROFILE TO SANDBOX' }), { target: { value: 'COPY PROFILE TO SANDBOX' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start sandbox copy' }))
    await waitFor(() => expect(microsoftPilot).toHaveBeenCalledWith('profile', 'source-user', 'reviewed-hash', 'COPY PROFILE TO SANDBOX'))
  })
})
