/**
 * The panel exists to answer the question the old 503 raised and could not:
 * "capacity is full" — full of WHAT, and where am I in the line?
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import QueuePanel, { ago } from './QueuePanel'
import * as client from '@/api/client'

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, fetchQueue: vi.fn(), cancelQueued: vi.fn() }
})

const snap = (over: Partial<client.QueueSnapshot> = {}): client.QueueSnapshot => ({
  capacity: 2,
  running: [{ jobName: 'migrate', startedAt: new Date().toISOString(),
              requestedBy: 'other@corp.example', mine: false }],
  waiting: [{ id: 7, jobName: 'seed', position: 1, mine: true,
              queuedAt: new Date().toISOString(), requestedBy: 'me@corp.example' }],
  recent: [],
  ...over,
})

beforeEach(() => {
  vi.mocked(client.fetchQueue).mockResolvedValue(snap())
  vi.mocked(client.cancelQueued).mockResolvedValue({ ok: true })
})
afterEach(() => vi.clearAllMocks())

describe('what is on the box', () => {
  it('names the job filling the slot, even though it is not mine', async () => {
    render(<QueuePanel />)
    expect(await screen.findByText('migrate')).toBeInTheDocument()
  })

  it('says how full the box is', async () => {
    render(<QueuePanel />)
    expect(await screen.findByText('1 of 2 slots busy')).toBeInTheDocument()
  })

  it('says who else it belongs to, so there is somebody to ask', async () => {
    render(<QueuePanel />)
    await waitFor(() =>
      expect(screen.getByText(/other@corp\.example/)).toBeInTheDocument())
  })
})

describe('where I am in the line', () => {
  it('shows my position', async () => {
    render(<QueuePanel />)
    expect(await screen.findByText(/#1 · seed/)).toBeInTheDocument()
  })

  it('says it starts on its own, so nobody sits there retrying', async () => {
    render(<QueuePanel />)
    await waitFor(() =>
      expect(screen.getByText(/starts on its own/)).toBeInTheDocument())
  })

  it('labels my own row as mine', async () => {
    render(<QueuePanel />)
    await waitFor(() => expect(screen.getByText(/yours/)).toBeInTheDocument())
  })
})

describe('cancelling', () => {
  it('drops my queued job by id', async () => {
    render(<QueuePanel />)
    const btn = await screen.findByRole('button')
    fireEvent.click(btn)
    expect(client.cancelQueued).toHaveBeenCalledWith(7)
  })

  it('offers no cancel button on somebody else\'s row', async () => {
    vi.mocked(client.fetchQueue).mockResolvedValue(snap({
      waiting: [{ id: 9, jobName: 'seed', position: 1, mine: false }],
    }))
    render(<QueuePanel />)
    await screen.findByText(/#1 · seed/)
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('an idle box', () => {
  it('says a job started now begins immediately', async () => {
    vi.mocked(client.fetchQueue).mockResolvedValue(
      snap({ running: [], waiting: [] }))
    render(<QueuePanel />)
    expect(await screen.findByText(/begins\s+immediately/)).toBeInTheDocument()
  })
})

describe('what happened to what I asked for', () => {
  it('shows why a queued job did not run', async () => {
    vi.mocked(client.fetchQueue).mockResolvedValue(snap({
      recent: [{ id: 3, jobName: 'seed', status: 'failed', mine: true,
                 detail: 'no service-account key on file' }],
    }))
    render(<QueuePanel />)
    expect(await screen.findByText(/no service-account key on file/))
      .toBeInTheDocument()
  })
})

describe('ago', () => {
  it('reads as elapsed time, not a timestamp to do arithmetic on', () => {
    expect(ago(new Date(Date.now() - 90_000).toISOString())).toBe('2m ago')
  })
  it('handles the naive stamps sqlite writes without a Z', () => {
    const iso = new Date(Date.now() - 30_000).toISOString().replace('Z', '')
    expect(ago(iso)).toMatch(/^\d+s ago$/)
  })
  it('is blank for nothing, not "NaN ago"', () => {
    expect(ago(undefined)).toBe('')
    expect(ago('not a date')).toBe('')
  })
})
