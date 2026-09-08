import { describe, it, expect, vi, beforeEach } from 'vitest'
import { runSeed } from './client'

/* `--groups` was advertised by /api/seed-scopes as a capability of this
   seeder and reachable from nowhere: runSeed took ten positional
   parameters, so passing an eleventh meant six `undefined`s at the one
   call site that needed it. A 200-user tenant was seeded with no groups,
   and so no group-typed Drive ACLs either. */

const fetchMock = vi.fn()
const body = () => JSON.parse(fetchMock.mock.calls[0][1].body)

describe('runSeed options', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    fetchMock.mockResolvedValue({ json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetchMock)
  })

  it('sends groups when asked', async () => {
    await runSeed('d.test', 'small', false, false, { groups: true })
    expect(body().groups).toBe(true)
  })

  it('omits it entirely when not asked', async () => {
    await runSeed('d.test', 'small', false, false)
    expect(body().groups).toBeUndefined()
  })

  it('still carries every other option', async () => {
    await runSeed('d.test', 'huge', true, false, {
      allUsers: true, workers: '12', localpartPrefix: 'r3-',
      sharedDrives: '2', users: 'a,b', createUntilFull: true,
    })
    const b = body()
    expect(b).toMatchObject({
      confirm_domain: 'd.test', scale: 'huge', create_users: true,
      all_users: true, workers: '12', localpart_prefix: 'r3-',
      shared_drives: '2', users: 'a,b', create_until_full: true,
    })
  })

  it('the four required arguments stay positional', async () => {
    await runSeed('d.test', 'tiny', true, true)
    expect(body()).toMatchObject({
      confirm_domain: 'd.test', scale: 'tiny',
      create_users: true, reset: true,
    })
  })
})
