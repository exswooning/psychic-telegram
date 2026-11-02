import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const fetchScheduledRuns = vi.fn()
const cancelScheduledRun = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchScheduledRuns: (...a: unknown[]) => fetchScheduledRuns(...a),
  cancelScheduledRun: (...a: unknown[]) => cancelScheduledRun(...a),
}))
import ScheduledRuns from './ScheduledRuns'

describe('ScheduledRuns', () => {
  beforeEach(() => { vi.clearAllMocks(); cancelScheduledRun.mockResolvedValue({ cancelled: 1 }) })

  it('shows nothing when no run is scheduled', async () => {
    fetchScheduledRuns.mockResolvedValue({ runs: [] })
    const { container } = render(<ScheduledRuns accountId={3} />)
    await waitFor(() => expect(fetchScheduledRuns).toHaveBeenCalledWith(3))
    expect(container.textContent).toBe('')
  })

  it('lists a pending run and cancels it', async () => {
    fetchScheduledRuns.mockResolvedValue({ runs: [
      { id: 7, accountId: 3, runAt: '2026-11-03T20:15:00Z', status: 'PENDING', detail: '', requestedBy: 'op' }] })
    render(<ScheduledRuns accountId={3} />)
    expect(await screen.findByTestId('scheduled-7')).toHaveTextContent('pending')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(cancelScheduledRun).toHaveBeenCalledWith(7, 3))
  })

  it('says why a run did not start', async () => {
    fetchScheduledRuns.mockResolvedValue({ runs: [
      { id: 8, accountId: 3, runAt: '2026-11-03T20:15:00Z', status: 'MISSED', detail: 'the API was not running at that hour', requestedBy: 'op' }] })
    render(<ScheduledRuns accountId={3} />)
    expect(await screen.findByTestId('scheduled-8')).toHaveTextContent('the API was not running')
  })
})
