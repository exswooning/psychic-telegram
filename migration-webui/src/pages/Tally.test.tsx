/**
 * The Tally page must never let an unchecked user, or a tally that measured nothing, read
 * as fine -- the same two rules One-to-one.test.tsx pins for its own page.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Tally from './Tally'
import type { TallyServiceCount, TallyView } from '@/api/controlPlane'

const cp = vi.hoisted(() => ({ view: vi.fn(), run: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 3 }),
  fetchTally: cp.view,
  runTally: cp.run,
}))

const svc = (over: Partial<TallyServiceCount> = {}): TallyServiceCount => ({
  source: 100, target: 100, skipped: 0, expected: 100, parity: 1.0, surplus: 0, ...over,
})
const view = (over: Partial<TallyView> = {}): TallyView => ({
  accountId: 3, onComplete: true,
  totals: { COMPLETE: 1, SHORT: 1, UNKNOWN: 1, NOT_TALLIED: 1 },
  users: [
    { user: 'ann@a.com', target: 'ann@b.com', status: 'DONE', verdict: 'COMPLETE', countParity: 1.0,
      recordedAt: '2026-09-27T10:00:00Z', services: { drive_files: svc() }, worst: [] },
    { user: 'bob@a.com', target: 'bob@b.com', status: 'DONE', verdict: 'SHORT', countParity: 0.8,
      recordedAt: '2026-09-27T10:00:00Z',
      services: { mail: svc({ target: 80, expected: 100, parity: 0.8 }) },
      worst: [{ user: 'bob@a.com', service: 'mail', source: 100, skipped: 0, expected: 100, target: 80, missing: 20 }] },
    { user: 'cy@a.com', target: 'cy@b.com', status: 'DONE', verdict: 'UNKNOWN', countParity: null,
      recordedAt: '2026-09-27T10:00:00Z', services: {}, worst: [] },
    { user: 'dee@a.com', target: 'dee@b.com', status: 'DONE', verdict: 'NOT_TALLIED', countParity: null,
      recordedAt: null, services: {}, worst: [] },
  ],
  ...over,
})
const show = () => render(<MemoryRouter initialEntries={['/tally?account=3']}><Tally /></MemoryRouter>)

beforeEach(() => {
  cp.view.mockReset().mockResolvedValue(view())
  cp.run.mockReset().mockResolvedValue({ ok: true, detail: 'started pid 1' })
})

describe('the verdicts', () => {
  it('shows a user nobody tallied as NOT TALLIED, never as fine', async () => {
    show()
    expect(await screen.findByTestId('verdict-dee@a.com')).toHaveTextContent('Not tallied')
    expect(screen.getByTestId('row-dee@a.com')).toHaveTextContent('finished — not tallied yet')
  })

  it('puts what is wrong first and the complete users last', async () => {
    show()
    await screen.findByTestId('row-ann@a.com')
    const order = screen.getAllByTestId(/^row-/).map((r) => r.getAttribute('data-testid'))
    expect(order).toEqual(['row-bob@a.com', 'row-cy@a.com', 'row-dee@a.com', 'row-ann@a.com'])
  })

  it('shows mail owed to the DMS as its own verdict, not as red Short', async () => {
    // 278 users waiting on the DMS and 22 whose mail never ran all read "Short".
    cp.view.mockResolvedValue(view({ users: [
      { user: 'eve@a.com', target: 'eve@b.com', status: 'DONE', verdict: 'OWED_TO_DMS', countParity: 0.2,
        recordedAt: '2026-09-27T10:00:00Z', services: { mail: svc({ target: 20, expected: 100, parity: 0.2 }) },
        worst: [] },
    ] }))
    show()
    const chip = await screen.findByTestId('verdict-eve@a.com')
    expect(chip).toHaveTextContent('Owed to DMS')
    expect(chip.className).toMatch(/colorInfo/)
  })

  it('shows unknown in amber, not green, and complete in green', async () => {
    expect(view().users[2].verdict).toBe('UNKNOWN')
    show()
    expect((await screen.findByTestId('verdict-cy@a.com')).className).toMatch(/colorWarning/)
    expect(screen.getByTestId('verdict-ann@a.com').className).toMatch(/colorSuccess/)
    expect(screen.getByTestId('verdict-bob@a.com').className).toMatch(/colorError/)
  })

  it('shows the parity percentage, not just the verdict', async () => {
    show()
    expect(await screen.findByTestId('row-bob@a.com')).toHaveTextContent('80.0%')
    expect(screen.getByTestId('row-cy@a.com')).toHaveTextContent('not measured')
  })

  it('counts every verdict in the header', async () => {
    show()
    expect(await screen.findByTestId('total-SHORT')).toHaveTextContent('1 short')
    expect(screen.getByTestId('total-NOT_TALLIED')).toHaveTextContent('1 not tallied')
  })
})

describe('what is found', () => {
  it('opens a user to show which service came up short and by how much', async () => {
    show()
    fireEvent.click(await screen.findByLabelText('show bob@a.com'))
    expect(await screen.findByText(/mail: 20 missing of 100 expected/)).toBeInTheDocument()
  })

  it('can be narrowed to the users that need attention', async () => {
    show()
    await screen.findByTestId('row-ann@a.com')
    fireEvent.click(screen.getByRole('button', { name: 'Needs attention' }))
    expect(screen.queryByTestId('row-ann@a.com')).not.toBeInTheDocument()
    expect(screen.getByTestId('row-bob@a.com')).toBeInTheDocument()
  })
})

describe('Tally now', () => {
  const confirm = async (reason = 'checking after the fix') => {
    fireEvent.click(await screen.findByTestId('tally-now'))
    fireEvent.change(await screen.findByLabelText('Reason Code'), { target: { value: reason } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
  }

  it('asks for every user by default', async () => {
    show()
    await confirm()
    await waitFor(() => expect(cp.run).toHaveBeenCalledWith('checking after the fix', { accountId: 3, users: [] }))
    expect(await screen.findByText('started pid 1')).toBeInTheDocument()
  })

  it('asks for just the users that were ticked', async () => {
    show()
    fireEvent.click(await screen.findByLabelText('select bob@a.com'))
    expect(screen.getByTestId('tally-now')).toHaveTextContent('Tally 1 selected')
    await confirm()
    await waitFor(() => expect(cp.run.mock.calls[0][1].users).toEqual(['bob@a.com']))
  })

  it('shows a refusal instead of pretending it started', async () => {
    cp.run.mockResolvedValue({ ok: false, detail: 'that migration belongs to another account' })
    show()
    await confirm()
    expect(await screen.findByText('that migration belongs to another account')).toBeInTheDocument()
  })
})

describe('the automatic check', () => {
  it('says so when it is switched off', async () => {
    cp.view.mockResolvedValue(view({ onComplete: false }))
    show()
    expect(await screen.findByText(/switched off for this account/)).toBeInTheDocument()
  })

  it('otherwise says it is exhaustive, not a sample', async () => {
    show()
    expect(await screen.findByText(/every item of every service, not a sample/)).toBeInTheDocument()
  })

  it('does not blank the page when the ledger cannot be read', async () => {
    cp.view.mockRejectedValue(new Error('boom'))
    show()
    expect(await screen.findByText('boom')).toBeInTheDocument()
    screen.getByText('Tally')
  })
})
