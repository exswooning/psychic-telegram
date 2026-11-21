import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchJob } from './client'

describe('a read that never answers', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('is given a timeout, so one hung request cannot hold a page on its loading bar', async () => {
    const seen: Array<RequestInit | undefined> = []
    vi.stubGlobal('fetch', vi.fn((_p: string, init?: RequestInit) => {
      seen.push(init)
      return Promise.resolve(new Response('{}', { status: 200 }))
    }))
    await fetchJob(0).catch(() => null)
    expect(seen[0]?.signal).toBeInstanceOf(AbortSignal)
  })

  it('reports itself as a timeout in plain words', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(Object.assign(new Error('x'), { name: 'TimeoutError' }))))
    await expect(fetchJob(0)).rejects.toThrow(/no answer after 20s/)
  })
})
