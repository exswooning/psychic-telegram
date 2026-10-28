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
