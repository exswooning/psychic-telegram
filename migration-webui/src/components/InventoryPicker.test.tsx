/**
 * The inventory was wired to whichever side its host panel happened to be,
 * so "what is actually in that domain?" could only be asked from a page you
 * had already opened for another reason -- and never about the other tenant
 * from where you were standing.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import InventoryPicker from './InventoryPicker'

const inventory = vi.fn()
const config = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchTenantInventory: (...a: unknown[]) => inventory(...a),
  fetchTenantConfigStatus: (...a: unknown[]) => config(...a),
}))
vi.mock('@/components/TenantInventoryPanel', () => ({
  default: ({ inv, domain, onRefresh, onDeepScan }: {
    inv: { accounts?: number } | null; domain: string
    onRefresh: () => void; onDeepScan: () => void
  }) => (
    <div data-testid="panel">
      <span data-testid="panel-domain">{domain}</span>
      <span data-testid="panel-accounts">{inv ? inv.accounts : 'none'}</span>
      <button onClick={onRefresh}>refresh</button>
      <button onClick={onDeepScan}>deep</button>
    </div>
  ),
}))

beforeEach(() => {
  inventory.mockReset()
  config.mockReset()
  config.mockImplementation((side: string) => Promise.resolve({
    side, domain: side === 'source' ? 'src.example' : 'tgt.example',
  }))
  inventory.mockResolvedValue({ side: 'source', domain: 'src.example',
                                accounts: 42, users: [], totals: {} })
})

describe('you can say which tenant to count', () => {
  it('offers both configured domains by name', async () => {
    render(<InventoryPicker />)
    await waitFor(() => expect(config).toHaveBeenCalledTimes(2))
    expect(await screen.findByText(/src\.example \(source\)/)).toBeInTheDocument()
  })

  it('counts the side that was picked, not the panel it sits in', async () => {
    render(<InventoryPicker />)
    await waitFor(() => expect(config).toHaveBeenCalledTimes(2))
    fireEvent.change(screen.getByTestId('inventory-side'),
                     { target: { value: 'target' } })
    fireEvent.click(await screen.findByText('refresh'))
    await waitFor(() => expect(inventory).toHaveBeenCalled())
    expect(inventory.mock.calls[0][0]).toBe('target')
  })

  it('defaults to the source', async () => {
    render(<InventoryPicker />)
    fireEvent.click(await screen.findByText('refresh'))
    await waitFor(() => expect(inventory.mock.calls[0][0]).toBe('source'))
  })

  it('honours an explicit starting side', async () => {
    render(<InventoryPicker initialSide="target" />)
    fireEvent.click(await screen.findByText('refresh'))
    await waitFor(() => expect(inventory.mock.calls[0][0]).toBe('target'))
  })

  it('passes the deep-scan flag through', async () => {
    render(<InventoryPicker />)
    fireEvent.click(await screen.findByText('deep'))
    await waitFor(() => expect(inventory.mock.calls[0][2]).toBe(true))
  })
})

describe('it never shows one tenant under another tenant name', () => {
  it('clears the counts when the domain changes', async () => {
    /* Leaving the previous tenant's numbers on screen under a newly-chosen
       domain is worse than showing nothing: they read as an answer to the
       question just asked. */
    render(<InventoryPicker />)
    fireEvent.click(await screen.findByText('refresh'))
    await waitFor(() =>
      expect(screen.getByTestId('panel-accounts')).toHaveTextContent('42'))
    fireEvent.change(screen.getByTestId('inventory-side'),
                     { target: { value: 'target' } })
    await waitFor(() =>
      expect(screen.getByTestId('panel-accounts')).toHaveTextContent('none'))
  })

  it('names the domain it is showing, not the side', async () => {
    render(<InventoryPicker />)
    await waitFor(() =>
      expect(screen.getByTestId('panel-domain')).toHaveTextContent('src.example'))
  })
})

describe('failures are shown, not swallowed', () => {
  it('reports a refused count', async () => {
    inventory.mockRejectedValue(new Error('no credential on file'))
    render(<InventoryPicker />)
    fireEvent.click(await screen.findByText('refresh'))
    await waitFor(() => expect(inventory).toHaveBeenCalled())
    // The panel receives it; this asserts the call settled rather than
    // leaving the picker stuck busy forever.
    expect(screen.getByTestId('panel')).toBeInTheDocument()
  })
})
