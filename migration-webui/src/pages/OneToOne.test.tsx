/**
 * The one-to-one page must never let an unchecked user, or a check that could not be made, read as fine;
 * and must say how much of a user a sample was.
 */
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import OneToOne from './OneToOne'
import type { OneToOneService, OneToOneView } from '@/api/controlPlane'

const cp = vi.hoisted(() => ({ view: vi.fn(), run: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 3 }),
  fetchOneToOne: cp.view,
  runOneToOne: cp.run,
}))

const svc = (service: string, verdict: OneToOneService['verdict'], extra: Partial<OneToOneService> = {}): OneToOneService => ({
  service, verdict, verifiedAt: '2026-09-26T15:00:00Z', checked: 25, identical: verdict === 'IDENTICAL' ? 25 : 23,
  sampledOf: 4180, ...extra,
})
const view = (over: Partial<OneToOneView> = {}): OneToOneView => ({
  accountId: 3, onComplete: true, perService: 25,
  totals: { IDENTICAL: 1, DIFFERENCES: 1, INCOMPLETE: 1, NOT_VERIFIED: 1 },
  users: [
    { user: 'ann@a.com', target: 'ann@b.com', status: 'DONE', verdict: 'IDENTICAL', verifiedAt: '2026-09-26T15:00:00Z', services: [svc('drive', 'IDENTICAL')] },
    { user: 'bob@a.com', target: 'bob@b.com', status: 'DONE', verdict: 'DIFFERENCES', verifiedAt: '2026-09-26T15:00:00Z',
      services: [svc('gmail', 'DIFFERENCES', {
        counts: { differences: 2 }, differences: [{ item: 'x', path: '/Projects/a.pdf', diffs: ['content differs when exported (10 -> 12 bytes)'] }] })] },
    { user: 'cy@a.com', target: 'cy@b.com', status: 'DONE', verdict: 'INCOMPLETE', verifiedAt: '2026-09-26T15:00:00Z',
      services: [svc('tasks', 'INCOMPLETE', { errors: ['tasks could not be verified: 403'] })] },
    { user: 'dee@a.com', target: 'dee@b.com', status: 'DONE', verdict: 'NOT_VERIFIED', verifiedAt: null, services: [] },
  ],
  ...over,
})
const show = () => render(<MemoryRouter initialEntries={['/one-to-one?account=3']}><OneToOne /></MemoryRouter>)

beforeEach(() => {
  cp.view.mockReset().mockResolvedValue(view())
  cp.run.mockReset().mockResolvedValue({ ok: true, detail: 'started pid 1' })
})

describe('the verdicts', () => {
  it('shows a user nobody checked as NOT VERIFIED, never as fine', async () => {
    show()
    expect(await screen.findByTestId('verdict-dee@a.com')).toHaveTextContent('Not verified')
    expect(screen.getByTestId('row-dee@a.com')).toHaveTextContent('finished — not checked yet')
  })

  it('puts what is wrong first and the identical users last', async () => {
    show()
    await screen.findByTestId('row-ann@a.com')
    const order = screen.getAllByTestId(/^row-/).map((r) => r.getAttribute('data-testid'))
    expect(order).toEqual(['row-bob@a.com', 'row-cy@a.com', 'row-dee@a.com', 'row-ann@a.com'])
  })

  it('shows an incomplete check in amber, not green', async () => {
    show()
    expect((await screen.findByTestId('verdict-cy@a.com')).className).toMatch(/colorWarning/)
    expect(screen.getByTestId('verdict-ann@a.com').className).toMatch(/colorSuccess/)
    expect(screen.getByTestId('verdict-bob@a.com').className).toMatch(/colorError/)
  })

  it('says how much of the user a sample was', async () => {
    show()
    expect(await screen.findAllByText(/drive 25\/25 of 4,180/)).toHaveLength(1)
  })

  it('counts every verdict in the header', async () => {
    show()
    expect(await screen.findByTestId('total-DIFFERENCES')).toHaveTextContent('1 differences')
    expect(screen.getByTestId('total-NOT_VERIFIED')).toHaveTextContent('1 not verified')
  })
})

describe('what is found', () => {
  it('opens a user to show the differences the checker kept', async () => {
    show()
    fireEvent.click(await screen.findByLabelText('show bob@a.com'))
    expect(await screen.findByText(/content differs when exported/)).toBeInTheDocument()
    expect(screen.getByText(/\/Projects\/a\.pdf/)).toBeInTheDocument()
  })

  it('does not promise more than it can show: strays are not counted, what is listed is', async () => {
    cp.view.mockResolvedValue(view({ users: [{
      user: 'eve@a.com', target: 'eve@b.com', status: 'DONE', verdict: 'DIFFERENCES', verifiedAt: '2026-09-26T15:00:00Z',
      services: [svc('drive', 'DIFFERENCES', {
        counts: { differences: 1, extras: 9 },            // nine scratch files on the target: informational
        differences: [{ item: 'x', path: '/a.pdf', diffs: ['modified time not preserved'] }],
        duplicates: [], notes: [] })],
    }] }))
    show()
    fireEvent.click(await screen.findByLabelText('show eve@a.com'))
    await screen.findByText(/modified time not preserved/)
    expect(screen.queryByText(/more; run/)).not.toBeInTheDocument()
  })

  it('lists a copy made twice, and says how many more there are', async () => {
    cp.view.mockResolvedValue(view({ users: [{
      user: 'eve@a.com', target: 'eve@b.com', status: 'DONE', verdict: 'DIFFERENCES', verifiedAt: '2026-09-26T15:00:00Z',
      services: [svc('gmail', 'DIFFERENCES', {
        counts: { duplicates: 30 },
        duplicates: Array.from({ length: 25 }, (_, i) => ({ messageId: `<m${i}@x>` })) })],
    }] }))
    show()
    fireEvent.click(await screen.findByLabelText('show eve@a.com'))
    expect(await screen.findByText(/copied twice — <m0@x>/)).toBeInTheDocument()
    expect(screen.getByText(/… 22 more; run/)).toBeInTheDocument()
  })

  it('shows why a check could not be made', async () => {
    show()
    fireEvent.click(await screen.findByLabelText('show cy@a.com'))
    expect(await screen.findByText(/could not check — tasks could not be verified: 403/)).toBeInTheDocument()
  })

  it('can be narrowed to the users that need attention', async () => {
    show()
    await screen.findByTestId('row-ann@a.com')
    fireEvent.click(screen.getByRole('button', { name: 'Needs attention' }))
    expect(screen.queryByTestId('row-ann@a.com')).not.toBeInTheDocument()
    expect(screen.getByTestId('row-bob@a.com')).toBeInTheDocument()
  })
})

describe('Verify now', () => {
  const confirm = async (reason = 'checking after the fix') => {
    fireEvent.click(await screen.findByTestId('verify-now'))
    fireEvent.change(await screen.findByLabelText('Reason Code'), { target: { value: reason } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
  }

  it('asks for every user by default, with the usual sample', async () => {
    show()
    await confirm()
    await waitFor(() => expect(cp.run).toHaveBeenCalledWith('checking after the fix', { accountId: 3, users: [], limit: undefined }))
    expect(await screen.findByText('started pid 1')).toBeInTheDocument()
  })

  it('asks for just the users that were ticked', async () => {
    show()
    fireEvent.click(await screen.findByLabelText('select bob@a.com'))
    expect(screen.getByTestId('verify-now')).toHaveTextContent('Verify 1 selected')
    await confirm()
    await waitFor(() => expect(cp.run.mock.calls[0][1].users).toEqual(['bob@a.com']))
  })

  it('asks for every item when told to', async () => {
    show()
    fireEvent.click(await screen.findByTestId('verify-now'))
    fireEvent.click(await screen.findByLabelText('check every item'))
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'full check' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(cp.run.mock.calls[0][1].limit).toBe(0))
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

  it('otherwise says how big a sample it takes', async () => {
    show()
    expect(await screen.findByText(/up to 25 of each kind of item/)).toBeInTheDocument()
  })

  it('does not blank the page when the ledger cannot be read', async () => {
    cp.view.mockRejectedValue(new Error('boom'))
    show()
    expect(await screen.findByText('boom')).toBeInTheDocument()
    within(document.body).getByText('One-to-one check')
  })
})
