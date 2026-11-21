import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
const calls = vi.hoisted(() => ({ users: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/client', () => ({ fetchUsers: calls.users, fetchStages: vi.fn().mockResolvedValue([]), fetchMetrics: vi.fn().mockRejectedValue(new Error('fixture')), fetchActivity: vi.fn().mockResolvedValue([]), fetchVerification: vi.fn().mockResolvedValue([]), fetchReport: vi.fn().mockResolvedValue(null) }))
import useMigration from './useMigration'
function Harness({ enabled }: { enabled: boolean }) { useMigration(enabled); return null }
describe('migration polling after authentication', () => {
  it('does not poll tenant data on a public sign-in page', async () => {
    calls.users.mockClear()
    const view = render(<Harness enabled={false} />)
    expect(calls.users).not.toHaveBeenCalled()
    view.rerender(<Harness enabled />)
    await waitFor(() => expect(calls.users).toHaveBeenCalledTimes(1))
  })
})

describe('migration polling does not wait for its slowest request', () => {
  it('shows the users the moment they arrive while another request is still pending', async () => {
    const { useMigrationStore } = await import('@/store')
    useMigrationStore.setState({ users: [], isLoading: true })
    let finishMetrics: (v: unknown) => void = () => {}
    calls.users.mockResolvedValueOnce([{ email: 'a@example.com' }])
    const client = await import('@/api/client')
    ;(client.fetchMetrics as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((r) => { finishMetrics = r }))
    render(<Harness enabled />)
    await waitFor(() => expect(useMigrationStore.getState().users).toHaveLength(1))
    expect(useMigrationStore.getState().isLoading).toBe(false)     // released by the first, not the last
    finishMetrics({ cpu: 1, ram: { percentage: 1 }, workers: { current: 1 }, uploadQueue: 0, retryQueue: 0 })
  })
})
