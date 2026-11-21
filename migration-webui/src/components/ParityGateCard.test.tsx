import React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import ParityGateCard from './ParityGateCard'

const api = vi.hoisted(() => ({ fetchParityGate: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({ fetchParityGate: api.fetchParityGate }))

describe('ParityGateCard', () => {
  beforeEach(() => api.fetchParityGate.mockReset())

  it('shows the verdict, what fails it and who acts, and what was not migrated', async () => {
    api.fetchParityGate.mockResolvedValue({
      accountId: 3, verdict: 'NOT_PARITY', users: 45, usersAtParity: 40,
      unverified: [{ user: 'a@s', service: 'gmail', verdict: 'STALE' }],
      differenceKinds: [{ kind: 'gmail.labels_added', how: 'auto', count: 12, users: ['a@s', 'b@s'] }],
      accepted: [], remainder: [{ status: 'DMS_FAILED', count: 17, why: 'Google refused these emails' }],
    })
    render(<ParityGateCard accountId={3} />)
    await waitFor(() => expect(screen.getByTestId('parity-gate-verdict')).toHaveTextContent('Not at parity'))
    expect(screen.getByText(/40 of 45 users at parity/)).toBeInTheDocument()
    expect(screen.getByTestId('parity-gate-kind-gmail.labels_added')).toHaveTextContent('Bitport repairs it')
    expect(screen.getByTestId('parity-gate-remainder')).toHaveTextContent('17 — Google refused these emails')
    expect(screen.getByTestId('parity-gate-unverified')).toHaveTextContent('1 check to run again (gmail)')
  })

  it('stays out of the way for a migration with no users yet', async () => {
    api.fetchParityGate.mockResolvedValue({ accountId: 3, verdict: 'UNVERIFIED', users: 0 })
    const { container } = render(<ParityGateCard accountId={3} />)
    await waitFor(() => expect(api.fetchParityGate).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})
