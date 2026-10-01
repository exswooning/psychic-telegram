import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

/* Shares owed to colleagues who have no target account yet: a run for a few users
   records them, and the operator should hear about it without opening a report. */

vi.mock('@/api/client', () => ({
  fetchJob: vi.fn().mockResolvedValue({ running: false, name: '', lines: [] }),
  fetchConfig: vi.fn().mockResolvedValue({ host: {} }),
  stopJob: vi.fn(),
}))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: vi.fn().mockResolvedValue({ id: 1, is_superadmin: true }),
  logout: vi.fn(),
  fetchOwedGrants: vi.fn().mockResolvedValue({ migrations: [{
    accountId: 3, accountName: 'Administrator', targetDomain: 'target2.example', shares: 9354, colleagues: 67,
    examples: ['seeduser141@t.example', 'seeduser214@t.example', 'seeduser231@t.example'] }] }),
}))

import Layout from './Layout'

describe('the notification for shares waiting on a colleague', () => {
  it('says how many, for whom, that they land on their own, and opens the migration', async () => {
    render(<MemoryRouter><Layout><div /></Layout></MemoryRouter>)
    await waitFor(() => expect(document.querySelector('.MuiBadge-badge')?.textContent).toBe('1'))
    fireEvent.click(screen.getByTestId('NotificationsIcon').closest('button')!)
    const item = await screen.findByTestId('notif-owed')
    expect(item).toHaveTextContent('target2.example: 9,354 shares waiting for 67 colleagues with no target account yet')
    expect(item).toHaveTextContent('seeduser141@t.example, seeduser214@t.example, seeduser231@t.example, …')
    expect(item).toHaveTextContent('granted automatically once those colleagues are migrated')
    fireEvent.click(item)
    expect(navigate).toHaveBeenCalledWith('/migrations/3')
  })
})
