import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetch: vi.fn(), start: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchMoveProbe: (...a: unknown[]) => api.fetch(...a),
  startMoveProbe: (...a: unknown[]) => api.start(...a),
}))
import MoveRouteProbe from './MoveRouteProbe'

const result = (route: string, ok: boolean | null, step = 'moved', detail = '') =>
  ({ route, ok, step, detail, status: ok === false ? 403 : null, testedAt: '2026-10-31T20:00:00Z' })
const probe = (results: ReturnType<typeof result>[], over = {}) =>
  ({ accountId: 3, pair: 's.example->t.example', results, moveAvailable: false, otherRouteOnly: false, ...over })

describe('Can files move between these tenants?', () => {
  beforeEach(() => { api.fetch.mockReset(); api.start.mockReset() })

  it('says it has not been tested, and does not offer a move', async () => {
    api.fetch.mockResolvedValue(probe([]))
    const onAvailable = vi.fn()
    render(<MoveRouteProbe accountId={3} onAvailable={onAvailable} />)
    await waitFor(() => expect(screen.getByTestId('move-probe-none')).toHaveTextContent('s.example->t.example'))
    expect(onAvailable).toHaveBeenLastCalledWith(false)
  })

  it('names the step Google refused, in Google\'s words, and keeps the move unavailable', async () => {
    api.fetch.mockResolvedValue(probe([
      result('direct', false, 'move it out of the source', 'HTTP 403 insufficientFilePermissions'),
      result('source-drive', false, 'the target admin takes it out', 'HTTP 403'),
    ]))
    const onAvailable = vi.fn()
    render(<MoveRouteProbe accountId={3} onAvailable={onAvailable} />)
    await waitFor(() => expect(screen.getByTestId('move-probe-direct')).toHaveTextContent('refused'))
    expect(screen.getByTestId('move-probe-direct')).toHaveTextContent('move it out of the source: HTTP 403 insufficientFilePermissions')
    expect(onAvailable).toHaveBeenLastCalledWith(false)
    expect(screen.queryByTestId('move-probe-available')).toBeNull()
  })

  it('offers the move once the direct route worked', async () => {
    api.fetch.mockResolvedValue(probe([result('direct', true), result('source-drive', true)], { moveAvailable: true }))
    const onAvailable = vi.fn()
    render(<MoveRouteProbe accountId={3} onAvailable={onAvailable} />)
    await waitFor(() => expect(screen.getByTestId('move-probe-available')).toBeTruthy())
    expect(onAvailable).toHaveBeenLastCalledWith(true)
  })

  it('says so when only the shared-drive route worked, since the engine cannot use it', async () => {
    api.fetch.mockResolvedValue(probe([result('direct', false, 'x', 'y'), result('source-drive', true)], { otherRouteOnly: true }))
    render(<MoveRouteProbe accountId={3} onAvailable={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('move-probe-other-only')).toBeTruthy())
  })

  it('starts the test with a reason and shows an upfront refusal', async () => {
    api.fetch.mockResolvedValue(probe([]))
    api.start.mockResolvedValue({ ok: false, detail: 'this account has a job running' })
    render(<MoveRouteProbe accountId={3} onAvailable={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('move-probe-run')).toBeTruthy())
    fireEvent.click(screen.getByTestId('move-probe-run'))
    await waitFor(() => expect(screen.getByText('this account has a job running')).toBeTruthy())
    expect(api.start).toHaveBeenCalledWith(expect.stringMatching(/move/), 3)
  })
})
