import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const svc = (service: string, checked: number, identical: number, verdict = 'IDENTICAL', counts = {}) =>
  ({ service, checked, identical, verdict, verifiedAt: '2026-10-23T08:00:00Z', sampledOf: null, scopeVersion: 2, counts })

const users = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 5 }),
  migrationOf: () => 5,
  // The pair as configured for the account: deliberately NOT the users' mail domains,
  // so the title is proven to come from here.
  fetchMigrations: () => Promise.resolve({ migrations: [
    { accountId: 6, sourceDomain: 's.example', targetDomain: 't.example' },
    { accountId: 5, sourceDomain: 'dharan.gov.np', targetDomain: 'mdharan.com' }] }),
  fetchOneToOne: () => Promise.resolve({
    accountId: 5, onComplete: true, perService: 25,
    totals: { IDENTICAL: 1, DIFFERENCES: 1 },
    users: [
      { user: 'info@old.example', target: 'info@new.example', status: 'DONE', verdict: 'DIFFERENCES',
        verifiedAt: '2026-10-23T08:00:00Z',
        services: [svc('gmail', 39772, 35443, 'DIFFERENCES', { differences: 4329, notCopied: 12 }),
                   svc('drive', 399, 395, 'DIFFERENCES', { differences: 9, errors: 34 })] },
      { user: 'cao@old.example', target: 'cao@new.example', status: 'DONE', verdict: 'IDENTICAL',
        verifiedAt: '2026-10-23T08:00:00Z', services: [svc('gmail', 3803, 3803)] },
    ],
  }),
  fetchTally: () => Promise.resolve({ accountId: 5, onComplete: false, totals: { COMPLETE: 1 },
    users: [{ user: 'cao@old.example', countParity: 1 }] }),
  fetchKnownIssues: () => Promise.resolve({ accountId: 5, recent: [], kinds: [
    { id: 'drive.sheets_scope', service: 'drive', title: 'A Sheet could not be read for comparison',
      cause: 'Read-only grant.', fix: 'spreadsheets.get ranges.', auto: true, open: 28, fixed: 0,
      lastFixed: null, openByCategory: { errors: 28 } },
    { id: 'gmail.labels_added', service: 'gmail', title: 'Gmail added a label the source message does not have',
      cause: 'Filed as received mail.', fix: 'Remove the extra labels on the target copy.', auto: true,
      open: 4329, fixed: 310, lastFixed: '2026-10-23T09:00:00Z', openByCategory: { differences: 4329 } },
    { id: 'drive.mtime', service: 'drive', title: 'Modified time differs', cause: '', fix: '', auto: true,
      open: 0, fixed: 0, lastFixed: null },
  ] }),
  fetchKnownIssueUsers: (id: string) => { users(id); return Promise.resolve({ issue: id, users: [
    { user: 'info@old.example', open: 4329, fixed: 0, lastFixed: null },
    { user: 'ward15@old.example', open: 0, fixed: 310, lastFixed: '2026-10-23T09:00:00Z' }] }) },
  fetchSourceGuard: () => Promise.resolve({ snapshotTakenAt: '2026-10-19', snapshotUsers: 45,
    latest: { verdict: 'CLEAN' } }),
}))

import PairParityCard from './PairParityCard'
import { parity } from '@/oneToOneVerdicts'

const show = () => render(<MemoryRouter><PairParityCard /></MemoryRouter>)

describe('pair parity card', () => {
  it('never rounds a shortfall up to 100%', () => {
    expect(parity(99999, 100000)).toBe('99.99%')
    expect(parity(5, 5)).toBe('100%')
    expect(parity(0, 0)).toBe('—')
  })

  it("is titled with the account's own configured pair, not the users' mail domains", async () => {
    show()
    await waitFor(() => expect(screen.getByTestId('pair-title')).toHaveTextContent('dharan.gov.np → mdharan.com'))
  })

  it('shows each user and every service compared', async () => {
    show()
    expect(await screen.findByTestId('pair-user-info@old.example')).toHaveTextContent('89.21%')
    expect(screen.getByTestId('pair-user-cao@old.example')).toHaveTextContent('100%')
    expect(screen.getByTestId('pair-svc-gmail')).toHaveTextContent('4,329')
    expect(screen.getByTestId('pair-items')).toHaveTextContent('39,641 of 43,974 items identical')
    expect(await screen.findByText(/source guard: CLEAN/)).toBeInTheDocument()
  })
})

describe('where the differences come from', () => {
  const open = async () => {
    show()
    fireEvent.click(await screen.findByTestId('where-differences'))
    return screen.findByTestId('differences-breakdown')
  }

  it('is closed until the card opens it', async () => {
    show()
    await screen.findByTestId('where-differences')
    expect(screen.queryByTestId('differences-breakdown')).not.toBeInTheDocument()
    expect(await open()).toBeInTheDocument()
  })

  it('has one bar per kind seen, largest first, with exact counts', async () => {
    const box = await open()
    const bars = within(box).getAllByTestId(/^kind-bar-/).map((b) => b.dataset.testid)
    expect(bars).toEqual(['kind-bar-gmail.labels_added', 'kind-bar-drive.sheets_scope'])   // mtime: never seen
    expect(screen.getByTestId('kind-count-gmail.labels_added')).toHaveTextContent('4,329 open · 310 fixed')
    expect(screen.getByTestId('kind-count-drive.sheets_scope')).toHaveTextContent("28 open · 0 fixed · 28 couldn't check")
  })

  it('answers "email or Drive?" per service, summed from the one-to-one rows', async () => {
    await open()
    expect(screen.getByTestId('svc-bar-gmail')).toHaveTextContent("4,329 differ · 0 missing · 12 copy or access findings · 0 couldn't check")
    expect(screen.getByTestId('svc-bar-drive')).toHaveTextContent("9 differ · 0 missing · 0 copy or access findings · 34 couldn't check")
  })

  it('a bar shows its cause, its fix and the users it affects', async () => {
    await open()
    fireEvent.click(screen.getByTestId('kind-bar-gmail.labels_added'))
    const detail = await screen.findByTestId('kind-detail')
    expect(detail).toHaveTextContent('Filed as received mail.')
    expect(detail).toHaveTextContent('Remove the extra labels on the target copy.')
    expect(await screen.findByTestId('kind-user-info@old.example')).toHaveTextContent('4,329 open · 0 fixed')
    expect(screen.getByTestId('kind-user-ward15@old.example')).toHaveTextContent('0 open · 310 fixed')
    expect(users).toHaveBeenCalledWith('gmail.labels_added')
  })
})
