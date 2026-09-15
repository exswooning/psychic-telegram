import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Migrations from './Migrations'

const fetchMigrations = vi.fn()
const fetchAllDomains = vi.fn()
const linkDomains = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMigrations: (...a: unknown[]) => fetchMigrations(...a),
  fetchAllDomains: (...a: unknown[]) => fetchAllDomains(...a),
  linkDomains: (...a: unknown[]) => linkDomains(...a),
}))

/**
 * The rule this page exists to respect: counts, never one percentage.
 * DONE / RUNNING / FAILED / PENDING coexist in every real batch, and
 * averaging them is what makes a half-failed run look half-done -- the one
 * reading that stops anybody investigating.
 */
const row = (over = {}) => ({
  accountId: 7,
  accountName: 'Rohit',
  sourceDomain: 'source.example.com',
  targetDomain: 'target.example.com',
  running: true,
  jobs: ['migrate'],
  progress: { users: 201, done: 19, running: 9, failed: 2, pending: 171,
              items: 149965, itemsFailed: 1 },
  ...over,
})

const show = (migrations: unknown[], extra = {}) => {
  fetchMigrations.mockResolvedValue({
    migrations, maxConcurrent: 2, activeTotal: 1, ...extra,
  })
  render(<MemoryRouter><Migrations /></MemoryRouter>)
}

describe('Migrations', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    fetchAllDomains.mockResolvedValue({ superadmin: true, domains: [
      { accountId: 66, accountEmail: 'a@x', side: 'source',
        domain: 'source.rohit.com', adminEmail: 'i@source.rohit.com',
        hasKey: true, clientId: '1' },
      { accountId: 66, accountEmail: 'a@x', side: 'target',
        domain: 'target.rohit.com', adminEmail: 'i@target.rohit.com',
        hasKey: true, clientId: '2' },
      { accountId: 68, accountEmail: 'b@x', side: 'target',
        domain: 'target.saraf.com', adminEmail: 'i@target.saraf.com',
        hasKey: false, clientId: '' },
    ] })
    linkDomains.mockResolvedValue({ ok: true, detail: 'source.rohit.com -> target.rohit.com' })
  })

  it('shows the tenant pair as source -> target', async () => {
    show([row()])
    await waitFor(() => expect(screen.getByTestId('migration-7')).toBeTruthy())
    const card = screen.getByTestId('migration-7')
    expect(card).toHaveTextContent('source.example.com')
    expect(card).toHaveTextContent('target.example.com')
  })

  it('reports every state separately, never averaged', async () => {
    show([row()])
    await waitFor(() => expect(screen.getByTestId('migration-7')).toBeTruthy())
    const card = screen.getByTestId('migration-7')
    expect(card).toHaveTextContent('19 users done')
    expect(card).toHaveTextContent('9 users running')
    expect(card).toHaveTextContent('171 pending')
    expect(card).toHaveTextContent('2 failed')
  })

  it('counts users against the real total, not the finished ones', async () => {
    show([row()])
    await waitFor(() => expect(screen.getByTestId('users-7')).toBeTruthy())
    expect(screen.getByTestId('users-7')).toHaveTextContent('19 of 201 users')
  })

  it('surfaces failed items rather than only the successful count', async () => {
    show([row()])
    await waitFor(() => expect(screen.getByTestId('items-7')).toBeTruthy())
    // Asserted on the values, not on one contiguous string: the count and
    // its label are separate elements so the count can carry the visual
    // weight, and an assertion spanning both breaks on styling rather than
    // on behaviour.
    const items = screen.getByTestId('items-7')
    expect(items).toHaveTextContent('149,965')
    expect(items).toHaveTextContent('items migrated')
    expect(items).toHaveTextContent('1 failed')
  })

  it('gives the item count more weight than the user chips', async () => {
    // Users are the wrong unit to watch: one takes hours, so those chips sit
    // still for a whole afternoon while hundreds of thousands of items move.
    // Live, this row read 17 users done across two days beside 457,385 items
    // migrated -- with the moving number in muted caption text at the far
    // right, a working migration was reported as making no progress.
    show([row()])
    await waitFor(() => expect(screen.getByTestId('items-7')).toBeTruthy())
    const count = screen.getByTestId('items-7').firstElementChild as HTMLElement
    expect(count.textContent).toBe('149,965')
    expect(Number.parseInt(getComputedStyle(count).fontWeight, 10)).toBeGreaterThanOrEqual(600)
  })

  it('marks a pair with no running job as idle', async () => {
    show([row({ running: false, jobs: [] })])
    await waitFor(() => expect(screen.getByTestId('migration-7')).toBeTruthy())
    expect(screen.getByTestId('migration-7')).toHaveTextContent('idle')
  })

  it('says when the concurrency cap is full, and why', async () => {
    /* Oversubscribing stalls both runs rather than slowing them -- every
       worker costs real memory. */
    show([row()], { maxConcurrent: 2, activeTotal: 2 })
    await waitFor(() => expect(screen.getByTestId('at-capacity')).toBeTruthy())
    expect(screen.getByTestId('at-capacity')).toHaveTextContent('2 of 2 slots in use')
  })

  it('does not claim capacity trouble when there is room', async () => {
    show([row()], { maxConcurrent: 2, activeTotal: 1 })
    await waitFor(() => expect(screen.getByTestId('migration-7')).toBeTruthy())
    expect(screen.queryByTestId('at-capacity')).toBeNull()
  })

  it('explains an empty list instead of showing a blank page', async () => {
    show([])
    await waitFor(() => expect(screen.getByTestId('no-migrations')).toBeTruthy())
    expect(screen.getByTestId('no-migrations')).toHaveTextContent('Setup Wizard')
  })

  it('offers to start a new migration', async () => {
    show([row()])
    await waitFor(() => expect(screen.getByTestId('new-migration')).toBeTruthy())
  })

  it('handles a pair set up on only one side', async () => {
    /* Half-configured is a real state -- source done, target not yet. */
    show([row({ targetDomain: '' })])
    await waitFor(() => expect(screen.getByTestId('migration-7')).toBeTruthy())
    expect(screen.getByTestId('migration-7')).toHaveTextContent('source.example.com')
  })
})


describe('connecting two set-up domains', () => {
  const open = async () => {
    fetchMigrations.mockResolvedValue({ migrations: [], maxConcurrent: 2, activeTotal: 0 })
    render(<MemoryRouter><Migrations /></MemoryRouter>)
    fireEvent.click(await screen.findByTestId('new-migration'))
    await screen.findByTestId('link-connect')
  }

  it('offers a picker of already-set-up domains, key-bearing only', async () => {
    await open()
    await waitFor(() => expect(fetchAllDomains).toHaveBeenCalled())
    // the keyless target.saraf.com must not be selectable
    // native select: the keyless target.saraf.com is not among the options
    const src = screen.getByTestId('link-source') as HTMLSelectElement
    const values = Array.from(src.options).map((o) => o.textContent || '')
    expect(values.some((v) => v.includes('source.rohit.com'))).toBe(true)
    expect(values.some((v) => v.includes('target.saraf.com'))).toBe(false)
  })

  it('links the two chosen domains, reusing their keys', async () => {
    await open()
    await waitFor(() => expect(fetchAllDomains).toHaveBeenCalled())
    // pick source
    fireEvent.change(screen.getByTestId('link-source'), { target: { value: '66:source:' } })
    fireEvent.change(screen.getByTestId('link-target'), { target: { value: '66:target:' } })
    fireEvent.change(screen.getByTestId('link-reason'),
                     { target: { value: 'rehearsal into the new tenant' } })
    fireEvent.click(screen.getByTestId('link-connect'))
    await waitFor(() => expect(linkDomains).toHaveBeenCalledWith(
      'rehearsal into the new tenant',
      { accountId: 66, side: 'source', supersededId: undefined },
      { accountId: 66, side: 'target', supersededId: undefined }))
  })

  it('will not connect without a reason', async () => {
    await open()
    await waitFor(() => expect(fetchAllDomains).toHaveBeenCalled())
    fireEvent.change(screen.getByTestId('link-source'), { target: { value: '66:source:' } })
    fireEvent.change(screen.getByTestId('link-target'), { target: { value: '66:target:' } })
    expect(screen.getByTestId('link-connect')).toBeDisabled()
  })

  it('surfaces a link failure instead of silently closing', async () => {
    linkDomains.mockResolvedValue({ ok: false, detail: 'source has no key on file' })
    await open()
    await waitFor(() => expect(fetchAllDomains).toHaveBeenCalled())
    fireEvent.change(screen.getByTestId('link-source'), { target: { value: '66:source:' } })
    fireEvent.change(screen.getByTestId('link-target'), { target: { value: '66:target:' } })
    fireEvent.change(screen.getByTestId('link-reason'),
                     { target: { value: 'trying this out' } })
    fireEvent.click(screen.getByTestId('link-connect'))
    expect(await screen.findByTestId('link-error')).toHaveTextContent('no key on file')
  })
})


describe('the picker offers overwritten domains too', () => {
  /* 009_superseded_configs.sql keeps an evicted domain so it "can be seen
     (and later re-linked)" -- the picker filtered exactly that out, so a
     pair whose source had since been replaced could not be expressed at
     all and the domain read as gone. It is selectable, marked, and carries
     its row id: (accountId, side) names the slot's CURRENT holder, so
     without the id this option would link whatever replaced it. */
  it('offers a superseded domain, marked, and links it by its own id', async () => {
    fetchAllDomains.mockResolvedValue({ superadmin: true, domains: [
      { accountId: 66, accountEmail: 'a@x', side: 'source',
        domain: 'source.rohit.com', adminEmail: 'i@source.rohit.com',
        hasKey: true, clientId: '1', superseded: false },
      { accountId: 68, accountEmail: 'b@x', side: 'source',
        domain: 'gone.saraf.com', adminEmail: 'i@gone.saraf.com',
        hasKey: true, clientId: '9', superseded: true, supersededId: 5,
        replacedBy: 'now.saraf.com' },
    ] })
    fetchMigrations.mockResolvedValue({ migrations: [], maxConcurrent: 2, activeTotal: 0 })
    render(<MemoryRouter><Migrations /></MemoryRouter>)
    fireEvent.click(await screen.findByTestId('new-migration'))
    await screen.findByTestId('link-connect')
    await waitFor(() => expect(fetchAllDomains).toHaveBeenCalled())
    const src = screen.getByTestId('link-source') as HTMLSelectElement
    const texts = Array.from(src.options).map((o) => o.textContent || '')
    expect(texts.some((t) => t.includes('source.rohit.com'))).toBe(true)
    expect(texts.some((t) => t.includes('gone.saraf.com')
                             && t.includes('(replaced)'))).toBe(true)

    // And picking it sends the row id, not just the slot it was evicted from.
    fireEvent.change(src, { target: { value: '68:source:5' } })
    fireEvent.change(screen.getByTestId('link-target'),
                     { target: { value: '66:source:' } })
    fireEvent.change(screen.getByTestId('link-reason'),
                     { target: { value: 'put the replaced domain back' } })
    fireEvent.click(screen.getByTestId('link-connect'))
    await waitFor(() => expect(linkDomains).toHaveBeenCalledWith(
      'put the replaced domain back',
      { accountId: 68, side: 'source', supersededId: 5 },
      { accountId: 66, side: 'source', supersededId: undefined }))
  })
})
