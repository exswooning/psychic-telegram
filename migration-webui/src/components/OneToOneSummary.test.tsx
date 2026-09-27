/**
 * The compact one-to-one section on the migration page: same rule as the full page
 * (never a blank, INCOMPLETE is amber not green), but only the totals and whoever
 * needs attention -- not every user.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import OneToOneSummary from './OneToOneSummary'

const api = vi.hoisted(() => ({ fetchOneToOne: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({ fetchOneToOne: api.fetchOneToOne }))

const view = (over = {}) => ({
  accountId: 3, onComplete: true, perService: 25,
  totals: { IDENTICAL: 3, DIFFERENCES: 0, INCOMPLETE: 0, NOT_VERIFIED: 297 },
  users: [
    { user: 'tom@a.com', target: 'tom@b.com', status: 'DONE', verdict: 'IDENTICAL', verifiedAt: '2026-09-27T05:50:25Z', services: [] },
  ],
  ...over,
})

// Braces: a hook that RETURNS a function has it called afterwards as cleanup, and mockReset()
// returns the mock -- without them this calls the mock a second time after every test.
beforeEach(() => { api.fetchOneToOne.mockReset() })

const put = (ui: React.ReactElement) => render(<MemoryRouter>{ui}</MemoryRouter>)

describe('OneToOneSummary', () => {
  it('shows the totals, worst first', async () => {
    api.fetchOneToOne.mockResolvedValue(view())
    put(<OneToOneSummary accountId={3} />)
    expect(await screen.findByTestId('o2o-summary-total-NOT_VERIFIED')).toHaveTextContent('297 not verified')
    expect(screen.getByTestId('o2o-summary-total-IDENTICAL')).toHaveTextContent('3 identical')
  })

  it('lists a user with differences, never as a blank', async () => {
    api.fetchOneToOne.mockResolvedValue(view({
      totals: { IDENTICAL: 2, DIFFERENCES: 1, INCOMPLETE: 0, NOT_VERIFIED: 0 },
      users: [{ user: 'george@a.com', target: 'george@b.com', status: 'DONE', verdict: 'DIFFERENCES',
                verifiedAt: '2026-09-27T06:00:00Z', services: [] }],
    }))
    put(<OneToOneSummary accountId={3} />)
    expect(await screen.findByText('george@a.com')).toBeInTheDocument()
  })

  it('shows an incomplete verdict in amber, never green', async () => {
    api.fetchOneToOne.mockResolvedValue(view({
      totals: { IDENTICAL: 0, DIFFERENCES: 0, INCOMPLETE: 1, NOT_VERIFIED: 0 },
      users: [{ user: 'nina@a.com', target: 'nina@b.com', status: 'DONE', verdict: 'INCOMPLETE',
                verifiedAt: null, services: [] }],
    }))
    put(<OneToOneSummary accountId={3} />)
    await screen.findByText('nina@a.com')
    const chip = screen.getAllByText('Incomplete')[0]
    expect(chip.closest('.MuiChip-root')?.className).toMatch(/colorWarning/)
  })

  it('stays out of the way when nothing has run yet', async () => {
    api.fetchOneToOne.mockResolvedValue(view({ totals: {}, users: [] }))
    const { container } = put(<OneToOneSummary accountId={3} />)
    await waitFor(() => expect(api.fetchOneToOne).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('links to the full page for this account', async () => {
    api.fetchOneToOne.mockResolvedValue(view())
    put(<OneToOneSummary accountId={3} />)
    expect(await screen.findByRole('link', { name: /view all/i })).toHaveAttribute('href', '/one-to-one?account=3')
  })

  it('says so when the check itself cannot be read', async () => {
    api.fetchOneToOne.mockRejectedValue(new Error('HTTP 500'))
    put(<OneToOneSummary accountId={3} />)
    expect(await screen.findByTestId('one-to-one-summary-error')).toHaveTextContent('HTTP 500')
  })
})
