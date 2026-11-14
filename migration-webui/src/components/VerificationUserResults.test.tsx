import React from 'react'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import VerificationUserResults from './VerificationUserResults'
import { fetchOneToOne } from '@/api/controlPlane'
import type { OneToOneView } from '@/api/controlPlane'

vi.mock('@/api/controlPlane', () => ({ fetchOneToOne: vi.fn() }))
const service = { service: 'gmail', verdict: 'IDENTICAL' as const, verifiedAt: '2026-11-11T10:00:00Z', checked: 20, identical: 20, sampledOf: 100, counts: { extras: 5 } }
const view: OneToOneView = { accountId: 3, onComplete: true, perService: 25, totals: {}, users: [
  { user: 'done@source', target: 'done@target', status: 'DONE', verdict: 'IDENTICAL', verifiedAt: service.verifiedAt, services: [service] },
  { user: 'pending@source', target: 'pending@target', status: 'DONE', verdict: 'NOT_VERIFIED', verifiedAt: null, services: [] },
  { user: 'partial@source', target: 'partial@target', status: 'DONE', verdict: 'INCOMPLETE', verifiedAt: service.verifiedAt,
    services: [{ ...service, service: 'drive', verdict: 'INCOMPLETE', counts: { errors: 1 } }] },
] }
beforeEach(() => { vi.resetAllMocks() })
const show = () => render(<MemoryRouter><VerificationUserResults accountId={3} /></MemoryRouter>)

it('shows saved users and verdicts without presenting extras or samples as full parity', async () => {
  vi.mocked(fetchOneToOne).mockResolvedValue(view)
  show()
  const done = await screen.findByTestId('saved-result-done@source')
  expect(within(done).getByText('Needs review')).toBeInTheDocument()
  expect(within(done).getByText(/Target-only items need review/)).toBeInTheDocument()
  expect(within(done).getByText(/Includes sampled checks/)).toBeInTheDocument()
  expect(within(screen.getByTestId('saved-result-partial@source')).getByText('Incomplete')).toBeInTheDocument()
  expect(screen.queryByTestId('saved-result-pending@source')).not.toBeInTheDocument()
  expect(screen.getByText(/2 of 3 users have saved results/)).toBeInTheDocument()
  expect(screen.getByText(/may be from earlier runs/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'View details' })).toHaveAttribute('href', '/one-to-one?account=3')
})

it('distinguishes no saved results from a failed fetch', async () => {
  vi.mocked(fetchOneToOne).mockResolvedValue({ ...view, users: [] })
  const { unmount } = show()
  expect(await screen.findByText('No user checks have been saved yet.')).toBeInTheDocument()
  unmount()
  vi.mocked(fetchOneToOne).mockRejectedValue(new Error('offline'))
  show()
  expect(await screen.findByText(/Could not refresh user results/)).toBeInTheDocument()
})
