import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Maintenance from './Maintenance'

const runResetTarget = vi.fn().mockResolvedValue({ ok: true })
vi.mock('@/api/client', () => ({
  fetchActions: vi.fn().mockResolvedValue([]),
  fetchConfig: vi.fn().mockResolvedValue({ config: { target_domain: 's.example' } }),
  runResetDriveLedger: vi.fn(), runWipeTarget: vi.fn(), runWipeSource: vi.fn(),
  runResetTarget: (...a: unknown[]) => runResetTarget(...a),
}))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: vi.fn().mockResolvedValue({ superadmin: false }),
  fetchAdminAccounts: vi.fn().mockResolvedValue([]),
}))

describe('Empty target data', () => {
  it('empties the typed target and keeps its accounts', async () => {
    render(<MemoryRouter><Maintenance /></MemoryRouter>)
    const go = await screen.findByTestId('empty-target-go')
    expect(go).toBeDisabled()
    fireEvent.change(screen.getByTestId('empty-target-domain'), { target: { value: 's.example' } })
    fireEvent.click(go)
    await waitFor(() => expect(runResetTarget).toHaveBeenCalledWith('s.example', undefined))
    expect(await screen.findByText(/Started/)).toBeTruthy()
  })
})
