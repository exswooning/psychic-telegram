import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const fetchExperiments = vi.fn()
const experimentAction = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchExperiments: (...a: unknown[]) => fetchExperiments(...a),
  experimentAction: (...a: unknown[]) => experimentAction(...a),
}))
import ExperimentPanel from './ExperimentPanel'

describe('ExperimentPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    experimentAction.mockResolvedValue({ ok: true, actionId: 1, detail: 'bookmarked 9 log(s)' })
  })

  it('bookmarks the named experiment for the account on screen', async () => {
    fetchExperiments.mockResolvedValue({ experiments: [] })
    render(<ExperimentPanel accountId={3} />)
    fireEvent.click(await screen.findByTestId('experiment-bookmark'))
    await waitFor(() => expect(experimentAction).toHaveBeenCalledWith('full45-r1', 'start', 3, expect.any(String)))
    expect(await screen.findByText('bookmarked 9 log(s)')).toBeTruthy()
  })

  it('builds the digest and shows it', async () => {
    fetchExperiments.mockImplementation((_a: number, name?: string) => Promise.resolve({
      experiments: [{ name: 'full45-r1', startedAt: '2026-11-02T15:00:00Z', building: false, digestAt: '2026-11-03T15:00:00Z' }],
      ...(name ? { digest: '# Experiment full45-r1\nNOT YET LEARNED' } : {}),
    }))
    render(<ExperimentPanel accountId={3} />)
    fireEvent.click(await screen.findByRole('button', { name: 'View' }))
    expect((await screen.findByTestId('experiment-digest')).textContent).toContain('NOT YET LEARNED')
    fireEvent.click(screen.getByRole('button', { name: 'Build digest' }))
    await waitFor(() => expect(experimentAction).toHaveBeenCalledWith('full45-r1', 'digest', 3, expect.any(String)))
  })

  it('refuses a name the server would refuse', async () => {
    fetchExperiments.mockResolvedValue({ experiments: [] })
    render(<ExperimentPanel accountId={3} />)
    fireEvent.change(await screen.findByTestId('experiment-name'), { target: { value: 'Bad Name' } })
    expect((screen.getByTestId('experiment-bookmark') as HTMLButtonElement).disabled).toBe(true)
  })
})
