import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const svc = (service: string, checked: number, identical: number, verdict = 'IDENTICAL', counts = {}) =>
  ({ service, checked, identical, verdict, verifiedAt: '2026-10-23T08:00:00Z', sampledOf: null, counts })

vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 5 }),
  migrationOf: () => 5,
  fetchOneToOne: () => Promise.resolve({
    accountId: 5, onComplete: true, perService: 25,
    totals: { IDENTICAL: 1, DIFFERENCES: 1 },
    users: [
      { user: 'info@dharan.gov.np', target: 'info@mdharan.com', status: 'DONE', verdict: 'DIFFERENCES',
        verifiedAt: '2026-10-23T08:00:00Z',
        services: [svc('gmail', 39772, 35443, 'DIFFERENCES', { differences: 4329 }), svc('drive', 399, 395, 'DIFFERENCES')] },
      { user: 'cao@dharan.gov.np', target: 'cao@mdharan.com', status: 'DONE', verdict: 'IDENTICAL',
        verifiedAt: '2026-10-23T08:00:00Z', services: [svc('gmail', 3803, 3803)] },
    ],
  }),
  fetchTally: () => Promise.resolve({ accountId: 5, onComplete: false, totals: { COMPLETE: 1 },
    users: [{ user: 'cao@dharan.gov.np', countParity: 1 }] }),
  fetchKnownIssues: () => Promise.resolve({ accountId: 5, recent: [],
    kinds: [{ id: 'gmail.labels_added', fixed: 310, open: 4329 }] }),
  fetchSourceGuard: () => Promise.resolve({ snapshotTakenAt: '2026-10-19', snapshotUsers: 45,
    latest: { verdict: 'CLEAN' } }),
}))

import PairParityCard from './PairParityCard'
import { parity } from '@/oneToOneVerdicts'

describe('pair parity card', () => {
  it('never rounds a shortfall up to 100%', () => {
    expect(parity(99999, 100000)).toBe('99.99%')
    expect(parity(5, 5)).toBe('100%')
    expect(parity(0, 0)).toBe('—')
  })

  it('names the pair and shows each user and every service compared', async () => {
    render(<MemoryRouter><PairParityCard /></MemoryRouter>)
    expect(await screen.findByTestId('pair-title')).toHaveTextContent('dharan.gov.np → mdharan.com')
    expect(screen.getByTestId('pair-user-info@dharan.gov.np')).toHaveTextContent('89.21%')
    expect(screen.getByTestId('pair-user-cao@dharan.gov.np')).toHaveTextContent('100%')
    expect(screen.getByTestId('pair-svc-gmail')).toHaveTextContent('4,329')
    expect(screen.getByTestId('pair-items')).toHaveTextContent('39,641 of 43,974 items identical')
    expect(await screen.findByText(/source guard: CLEAN/)).toBeInTheDocument()
    expect(await screen.findByText(/310 findings fixed, 4,329 open/)).toBeInTheDocument()
  })
})
