import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

/* Mirror sits in the sidebar under Migrate, straight after Migrations: it is what a
   finished migration carries on as, for the same pair. */

vi.mock('@/api/client', () => ({
  fetchJob: vi.fn().mockResolvedValue({ running: false, name: '', lines: [] }),
  fetchConfig: vi.fn().mockResolvedValue({ host: {} }),
  stopJob: vi.fn(),
}))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: vi.fn().mockResolvedValue({ id: 1, is_superadmin: false }),
  logout: vi.fn(),
}))

import Layout from './Layout'

describe('the Mirror sidebar entry', () => {
  it('is under Migrate, right after Migrations, and opens /mirror', async () => {
    render(<MemoryRouter><Layout><div /></Layout></MemoryRouter>)
    const mirror = (await screen.findAllByText('Mirror'))[0]
    const migrations = screen.getAllByText('Migrations')[0]
    const jobs = screen.getAllByText('Jobs')[0]
    const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
    expect(follows(migrations, mirror)).toBe(true)
    expect(follows(mirror, jobs)).toBe(true)
    fireEvent.click(mirror)
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/mirror'))
  })
})
