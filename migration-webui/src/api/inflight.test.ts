import { describe, it, expect } from 'vitest'
import { coalesce, inflightCount } from './inflight'

const deferred = <T,>() => {
  let resolve!: (v: T) => void, reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('coalesce', () => {
  it('starts one request for callers that overlap', async () => {
    const d = deferred<string>()
    let starts = 0
    const start = () => { starts++; return d.promise }

    const a = coalesce('/x', start)
    const b = coalesce('/x', start)
    d.resolve('one')

    expect(await a).toBe('one')
    expect(await b).toBe('one')
    expect(starts).toBe(1)
  })

  it('is not a cache -- a later call goes to the network again', async () => {
    let starts = 0
    const start = () => { starts++; return Promise.resolve(starts) }
    expect(await coalesce('/y', start)).toBe(1)
    expect(await coalesce('/y', start)).toBe(2)
  })

  it('lets go after a failure, so a poll can recover', async () => {
    const boom = () => Promise.reject(new Error('nope'))
    await expect(coalesce('/z', boom)).rejects.toThrow('nope')
    expect(inflightCount()).toBe(0)
    expect(await coalesce('/z', () => Promise.resolve('back'))).toBe('back')
  })

  it('keeps different paths apart', async () => {
    const d1 = deferred<string>(), d2 = deferred<string>()
    const p1 = coalesce('/a', () => d1.promise)
    const p2 = coalesce('/b', () => d2.promise)
    expect(inflightCount()).toBe(2)
    d1.resolve('a'); d2.resolve('b')
    expect([await p1, await p2]).toEqual(['a', 'b'])
  })
})
