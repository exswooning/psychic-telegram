/**
 * A perf test re-runs a few users between settings. Emptying the whole
 * target for that is hours; the users under test alone is minutes.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

const runResetTarget = vi.fn().mockResolvedValue({ ok: true })
vi.mock('@/api/client', () => ({
  fetchStatus: () => Promise.resolve({ steps: [] }),
  checkStep: () => Promise.resolve({ ok: true }),
  fetchActions: () => Promise.resolve({}),
  fetchDwd: () => Promise.resolve({}),
  runSeed: () => Promise.resolve({ ok: true }),
  runResetTarget: (...a: unknown[]) => runResetTarget(...a),
}))
vi.mock('@/api/controlPlane', () => ({}))
vi.mock('@/components/JobProgress', () => ({ default: () => null }))

import { ResetTargetStep } from './SeedWizard'

describe('resetting only some users', () => {
  it('sends the users and services it was given', async () => {
    render(<ResetTargetStep />)
    fireEvent.change(screen.getByLabelText('Type the target domain to confirm'),
                     { target: { value: 'tgt.example.com' } })
    fireEvent.change(screen.getByTestId('reset-users'),
                     { target: { value: 'a@src.example.com' } })
    fireEvent.change(screen.getByTestId('reset-services'), { target: { value: 'drive' } })
    fireEvent.click(screen.getByTestId('reset-target'))
    expect(await screen.findByText('a@src.example.com')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('reset-target-go'))
    await waitFor(() => expect(runResetTarget).toHaveBeenCalledWith(
      'tgt.example.com', undefined, { users: 'a@src.example.com', services: 'drive' }))
  })
})
