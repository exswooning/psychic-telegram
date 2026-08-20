import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Migrations from './Migrations'

const fetchMigrations = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMigrations: (...a: unknown[]) => fetchMigrations(...a),
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
  beforeEach(() => { vi.clearAllMocks() })

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
    expect(card).toHaveTextContent('19 done')
    expect(card).toHaveTextContent('9 running')
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
    await waitFor(() => expect(screen.getByTestId('migration-7')).toBeTruthy())
    expect(screen.getByTestId('migration-7')).toHaveTextContent('149,965 items migrated')
    expect(screen.getByTestId('migration-7')).toHaveTextContent('1 failed')
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
