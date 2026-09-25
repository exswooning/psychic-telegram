/**
 * seed_sandbox.py has no "already seeded" check of its own -- every run is
 * additive, on top of whoever already exists. This is the UI that makes
 * that safe by construction: no create-users, no reset, no all-users/
 * create-until-full. What it must not do is quietly grow the ability to
 * create accounts or delete anything -- that is the one line that must
 * never move without someone deciding to move it.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import SeedTopUp from './SeedTopUp'
import * as client from '@/api/client'

vi.mock('@/api/controlPlane', () => ({
  fetchDomainGuardStatus: () => Promise.resolve({ domain: 'x', protected: false }),
}))

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, runSeed: vi.fn(), fetchStorageSummary: vi.fn(() => Promise.resolve({
    error: '', skus: [{ skuId: '1010020028', name: 'Business Standard', accounts: 300,
                        sampleUser: 'a@x', limitBytes: 2e12, error: '' }] })) }
})

beforeEach(() => {
  vi.mocked(client.runSeed).mockReset()
  vi.mocked(client.runSeed).mockResolvedValue({ ok: true })
})

describe('gated like every other action that writes to a tenant', () => {
  it('will not run until the domain is typed', async () => {
    render(<SeedTopUp domain="src.example" />)
    expect(screen.getByRole('button', { name: 'Add more' })).toBeDisabled()
  })

  it('runs once something is typed', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
  })

  it('carries the account id through when the domain came from a picker', async () => {
    /* Omitting it resolves the request against whoever is SIGNED IN, not
       the domain topped up -- "set the source domain in step 2 first"
       against the wrong tenant, for a superadmin working someone else's
       account. */
    render(<SeedTopUp domain="src.example" accountId={68} />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [, , , , opts] = vi.mocked(client.runSeed).mock.calls[0]
    expect(opts).toMatchObject({ accountId: 68 })
  })
})

describe('never creates accounts and never deletes anything', () => {
  it('always sends createUsers and reset as false', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [domain, , createUsers, reset] = vi.mocked(client.runSeed).mock.calls[0]
    expect(domain).toBe('src.example')
    expect(createUsers).toBe(false)
    expect(reset).toBe(false)
  })

  it('has no control that could create or delete users', () => {
    // Not "unchecked" -- ABSENT. A checkbox that exists and defaults off
    // can be turned on; these controls do not exist on this form at all.
    render(<SeedTopUp domain="src.example" />)
    expect(screen.queryByLabelText(/create users/i)).toBeNull()
    expect(screen.queryByLabelText(/create until full/i)).toBeNull()
    expect(screen.queryByLabelText(/all users/i)).toBeNull()
    expect(screen.queryByText(/reset/i)).toBeNull()
  })
})

describe('what it adds', () => {
  it('defaults to every service', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [, , , , opts] = vi.mocked(client.runSeed).mock.calls[0]
    expect(opts?.only).toBeUndefined()
  })

  it('narrows to one service when picked', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.change(screen.getByTestId('topup-only'), { target: { value: 'chat' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [, , , , opts] = vi.mocked(client.runSeed).mock.calls[0]
    expect(opts?.only).toBe('chat')
  })

  it('says plainly that it adds rather than replaces', () => {
    render(<SeedTopUp domain="src.example" />)
    expect(screen.getByText(/adds more content/i)).toBeInTheDocument()
    expect(screen.getByText(/left alone/i)).toBeInTheDocument()
  })
})

describe('filling storage until full', () => {
  it('is off by default -- an ordinary top-up is unaffected', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add more' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [, , , , opts] = vi.mocked(client.runSeed).mock.calls[0]
    expect(opts?.fillUntilFull).toBeUndefined()
    expect(opts?.topUpOnly).toBeUndefined()
  })

  it('sends topUpOnly and fillUntilFull together when checked', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'),
                     { target: { value: 'src.example' } })
    fireEvent.click(screen.getByTestId('topup-fill-until-full'))
    fireEvent.click(screen.getByRole('button', { name: 'Fill until full' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    const [, , , , opts] = vi.mocked(client.runSeed).mock.calls[0]
    expect(opts?.fillUntilFull).toBe(true)
    expect(opts?.topUpOnly).toBe(true)
  })

  it('disables the content-volume controls while checked', () => {
    /* seed_sandbox.py's --top-up-only skips every other seeding step, so
       leaving these editable would suggest they still do something. */
    render(<SeedTopUp domain="src.example" />)
    fireEvent.click(screen.getByTestId('topup-fill-until-full'))
    expect(screen.getByTestId('topup-only')).toBeDisabled()
    expect(screen.getByTestId('topup-scale')).toBeDisabled()
    expect(screen.getByTestId('topup-shared-drives')).toBeDisabled()
  })

  it('relabels the button so it says what pressing it will do', () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.click(screen.getByTestId('topup-fill-until-full'))
    expect(screen.getByRole('button', { name: 'Fill until full' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add more' })).toBeNull()
  })
})

describe('the percentage and what it is a percentage of', () => {
  it('shows the licence, its storage, and the fill target, and sends the %', async () => {
    render(<SeedTopUp domain="src.example" />)
    fireEvent.change(screen.getByTestId('topup-domain'), { target: { value: 'src.example' } })
    fireEvent.click(screen.getByTestId('topup-fill-until-full'))
    fireEvent.change(screen.getByTestId('topup-fill-percent'), { target: { value: '25' } })
    const line = await screen.findByTestId('topup-sku-1010020028')
    expect(line).toHaveTextContent('Business Standard')
    expect(line).toHaveTextContent('2,000 GB each')
    expect(line).toHaveTextContent('500 GB')
    fireEvent.click(screen.getByRole('button', { name: 'Fill until full' }))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
    expect(vi.mocked(client.runSeed).mock.calls[0][4]?.fillPercent).toBe(25)
  })

  it('warns, before the run, how big and how slow an ambitious percentage is', async () => {
    /* 100% of 2 TB across 300 accounts is 600 TB; at Google's ~750 GB per
       account per day that cannot take less than 3 days. Found the hard way:
       a 100% fill of 9 TB accounts sat at 0/300 for two hours. */
    render(<SeedTopUp domain="src.example" />)
    fireEvent.click(screen.getByTestId('topup-fill-until-full'))
    await screen.findByTestId('topup-sku-1010020028')
    fireEvent.change(screen.getByTestId('topup-fill-percent'), { target: { value: '100' } })
    const warn = await screen.findByTestId('topup-volume')
    expect(warn).toHaveTextContent('600.0 TB')
    expect(warn).toHaveTextContent('at least 3 days')
    expect(warn).toHaveTextContent('37% or less fits in a day')
    // A percentage that fits in a day raises no warning.
    fireEvent.change(screen.getByTestId('topup-fill-percent'), { target: { value: '25' } })
    await waitFor(() => expect(screen.queryByTestId('topup-volume')).toBeNull())
  })
})
