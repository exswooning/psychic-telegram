import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

/* The regression set makes new target accounts, so it runs only for the account picked in
   "Viewing account" -- never for whichever account the session happens to resolve to. */

const seen: Array<Record<string, unknown>> = []
vi.mock('@/api/client', () => ({
  fetchActions: vi.fn().mockResolvedValue({ regression: { label: 'Regression set', blurb: '', destructive: false } }),
}))
vi.mock('@/components/JobRunner', () => ({
  default: (p: Record<string, unknown>) => { seen.push(p); return <div data-testid="job-runner" /> },
}))
vi.mock('@/components/AbBenchmarkPanel', () => ({ default: () => null }))
vi.mock('@/components/BenchmarkRunner', () => ({ default: () => null }))

import Benchmarks from './Benchmarks'

describe('the regression set on the Benchmarks page', () => {
  it('asks for an account before it offers to run', async () => {
    render(<MemoryRouter initialEntries={['/benchmarks']}><Benchmarks /></MemoryRouter>)
    expect(await screen.findByText(/Pick the account/)).toBeTruthy()
    expect(screen.queryByTestId('job-runner')).toBeNull()
  })

  it('runs for the viewed account', async () => {
    render(<MemoryRouter initialEntries={['/benchmarks?account=3']}><Benchmarks /></MemoryRouter>)
    expect(await screen.findByTestId('job-runner')).toBeTruthy()
    expect(seen[seen.length - 1]).toMatchObject({ name: 'regression', accountId: 3 })
  })
})
