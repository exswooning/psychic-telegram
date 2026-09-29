/**
 * Discovery's own worker count (the perf plan's test 9). It used to borrow the
 * migration's count; `discover` alone now takes one per run, blank = as before.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import JobRunner from './JobRunner'
import * as client from '@/api/client'

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, fetchJob: vi.fn(), runAction: vi.fn() }
})

const SPEC = { label: 'Discover', blurb: 'Read-only scan.', destructive: false, confirm: '' }

beforeEach(() => {
  vi.mocked(client.runAction).mockReset().mockResolvedValue({ ok: true, error: null })
  vi.mocked(client.fetchJob).mockReset().mockResolvedValue({
    running: false, name: '', rc: null, elapsed: 0, lines: [], total: 0,
    progressPct: null, etaSeconds: null, external: false,
  })
})

describe('discover takes its own worker count', () => {
  it('sends the count that was typed', async () => {
    render(<JobRunner name="discover" spec={SPEC} />)
    fireEvent.change(await screen.findByTestId('discover-workers'), { target: { value: '32' } })
    screen.getByRole('button', { name: /discover/i }).click()
    await waitFor(() => expect(client.runAction).toHaveBeenCalled())
    expect(vi.mocked(client.runAction).mock.calls[0][3]).toEqual({ workers: 32 })
  })

  it('left blank, sends nothing extra', async () => {
    render(<JobRunner name="discover" spec={SPEC} />)
    ;(await screen.findByRole('button', { name: /discover/i })).click()
    await waitFor(() => expect(client.runAction).toHaveBeenCalled())
    expect(vi.mocked(client.runAction).mock.calls[0][3]).toBeUndefined()
  })

  it('is offered for discover only', async () => {
    render(<JobRunner name="shared_drives_inventory" spec={{ ...SPEC, label: 'Inventory' }} />)
    await screen.findByRole('button', { name: /inventory/i })
    expect(screen.queryByTestId('discover-workers')).toBeNull()
  })
})
