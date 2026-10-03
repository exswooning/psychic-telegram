import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import LearnedLimits from './LearnedLimits'

const fetchRateCeilings = vi.fn()
const forgetRateCeiling = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchRateCeilings: (...a: unknown[]) => fetchRateCeilings(...a),
  forgetRateCeiling: (...a: unknown[]) => forgetRateCeiling(...a),
}))

describe('learned rate limits', () => {
  beforeEach(() => {
    fetchRateCeilings.mockReset(); forgetRateCeiling.mockReset()
    fetchRateCeilings.mockResolvedValue({ accountId: 3, ceilings: [
      { tenant: 'source', ceiling: 281.23, updated_at: '2026-09-28T20:30:07Z' }] })
  })

  it('lists what each side has proven', async () => {
    render(<LearnedLimits accountId={3} />)
    expect(await screen.findByTestId('ceiling-source')).toHaveTextContent('281.2 calls/s')
    expect(fetchRateCeilings).toHaveBeenCalledWith(3)
  })

  it('forgets one, with a reason', async () => {
    forgetRateCeiling.mockResolvedValue({ ok: true, actionId: 1, detail: 'forgot the learned source ceiling' })
    render(<LearnedLimits accountId={3} />)
    fireEvent.click(await screen.findByTestId('forget-source'))
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'learned from a 403' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(forgetRateCeiling).toHaveBeenCalledWith(3, 'source', 'learned from a 403'))
  })
})
