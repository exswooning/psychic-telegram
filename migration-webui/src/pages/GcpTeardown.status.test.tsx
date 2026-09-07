import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

/* A phase whose ACTION succeeded and whose CHECK could not run is neither
   "ok" nor "failed". Live, the DWD revoke deleted the delegation, the
   re-read of the list aborted, and the panel showed a red "failed" -- so an
   operator went hunting in Admin Console for a row that was not there. */

const fetchTeardownStatus = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  startTeardown: vi.fn(),
  fetchTeardownStatus: (...a: unknown[]) => fetchTeardownStatus(...a),
  fetchTeardownKnown: vi.fn().mockResolvedValue({ tenants: [] }),
}))

import GcpTeardown from './GcpTeardown'

const withPhases = (...p: { name: string; status: string; detail?: string }[]) => ({
  running: false,
  result: { ok: p.every((x) => x.status === 'ok'), phases: p },
})

function mount(...p: { name: string; status: string; detail?: string }[]) {
  fetchTeardownStatus.mockResolvedValue(withPhases(...p))
  return render(<GcpTeardown />)
}

describe('teardown result', () => {
  it('calls a run with an unverified phase "unverified", not "failed"', async () => {
    mount({ name: 'revoke DWD delegation', status: 'unverified' },
          { name: 'delete project', status: 'ok' })
    expect(await screen.findByText('unverified')).toBeInTheDocument()
    expect(screen.queryByText('failed')).toBeNull()
  })

  it('still says failed when something actually failed', async () => {
    mount({ name: 'revoke DWD delegation', status: 'failed' },
          { name: 'delete project', status: 'ok' })
    expect(await screen.findByText('failed')).toBeInTheDocument()
  })

  it('prefers failed over unverified when both are present', async () => {
    mount({ name: 'a', status: 'unverified' }, { name: 'b', status: 'failed' })
    expect(await screen.findByText('failed')).toBeInTheDocument()
    expect(screen.queryByText('unverified')).toBeNull()
  })

  it('tells the operator that re-running is the check', async () => {
    mount({ name: 'revoke DWD delegation', status: 'unverified' })
    expect(await screen.findByText(/Re-running is the check/i)).toBeInTheDocument()
  })

  it('marks the phase line distinctly from ok and FAIL', async () => {
    mount({ name: 'revoke DWD delegation', status: 'unverified' })
    expect(await screen.findByText(/\?\?\?\?\s+revoke DWD delegation/)).toBeInTheDocument()
  })

  it('an all-ok run is unchanged', async () => {
    mount({ name: 'delete project', status: 'ok' })
    expect(await screen.findByText('ok')).toBeInTheDocument()
  })
})
