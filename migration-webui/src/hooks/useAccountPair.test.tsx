import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

const cp = vi.hoisted(() => ({ fetchMigrations: vi.fn() }))
vi.mock('@/api/controlPlane', () => cp)
import { useAccountPair } from './useAccountPair'

describe('naming the account an action will act on', () => {
  it('gives "source → target" for the account asked about, never another', async () => {
    cp.fetchMigrations.mockResolvedValue({ migrations: [
      { accountId: 2, sourceDomain: 'gorkhatweed.com', targetDomain: 'new.gorkhatweed.com' },
      { accountId: 3, sourceDomain: 's.example', targetDomain: 't.example' }] })
    const { result } = renderHook(() => useAccountPair(3))
    await waitFor(() => expect(result.current).toBe('s.example → t.example'))
  })
  it('gives nothing, not a guess, while unknown or for an account that is not listed', async () => {
    cp.fetchMigrations.mockResolvedValue({ migrations: [] })
    const { result } = renderHook(() => useAccountPair(9))
    await new Promise((r) => setTimeout(r, 30))
    expect(result.current).toBe('')
    expect(renderHook(() => useAccountPair(undefined)).result.current).toBe('')
  })
})
