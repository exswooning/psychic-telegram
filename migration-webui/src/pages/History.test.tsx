/**
 * History must never let an unobserved exit read as a clean one, and must show
 * every job kind it is handed rather than a hardcoded allowlist -- the ledger's
 * own record of "what happened", not a curated summary of it.
 */
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import History from './History'
import type { HistoryRun, HistoryView } from '@/api/controlPlane'

const cp = vi.hoisted(() => ({ view: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 3 }),
  fetchHistory: cp.view,
}))

const run = (over: Partial<HistoryRun> = {}): HistoryRun => ({
  jobName: 'migrate', pid: 100, startedAt: '2026-09-26T01:00:00Z',
  finishedAt: '2026-09-26T02:00:00Z', rc: 0, detail: '', running: false, ...over,
})
const view = (runs: HistoryRun[]): HistoryView => ({ accountId: 3, runs })
const show = () => render(<MemoryRouter initialEntries={['/history?account=3']}><History /></MemoryRouter>)

beforeEach(() => { cp.view.mockReset().mockResolvedValue(view([run()])) })

describe('outcomes', () => {
  it('reads a clean exit as finished', async () => {
    cp.view.mockResolvedValue(view([run({ rc: 0 })]))
    show()
    expect(await screen.findByText('finished')).toBeInTheDocument()
  })

  it('reads a negative exit code as a crash, naming the signal, never a plain failure', async () => {
    cp.view.mockResolvedValue(view([run({ rc: -6 })]))
    show()
    expect(await screen.findByText('crashed (signal 6)')).toBeInTheDocument()
  })

  it('reads a positive exit code as failed', async () => {
    cp.view.mockResolvedValue(view([run({ rc: 2 })]))
    show()
    expect(await screen.findByText('failed (exit 2)')).toBeInTheDocument()
  })

  it('reads an exit that was never observed as unknown, never as a pass', async () => {
    cp.view.mockResolvedValue(view([run({ rc: null, finishedAt: '2026-09-26T02:00:00Z' })]))
    show()
    expect(await screen.findByText('unknown')).toBeInTheDocument()
  })

  it('reads a run still going as running, regardless of what rc happens to hold', async () => {
    cp.view.mockResolvedValue(view([run({ running: true, finishedAt: null, rc: null })]))
    show()
    expect(await screen.findByText('running')).toBeInTheDocument()
  })
})

describe('what it shows', () => {
  it('lists whatever job kinds it is handed, not a fixed set', async () => {
    cp.view.mockResolvedValue(view([
      run({ jobName: 'repair' }), run({ jobName: 'dms' }), run({ jobName: 'trim-filler' }),
    ]))
    show()
    expect(await screen.findByText('repair')).toBeInTheDocument()
    expect(screen.getByText('dms')).toBeInTheDocument()
    expect(screen.getByText('trim-filler')).toBeInTheDocument()
  })

  it('says when nothing has ever run, rather than an empty table with no explanation', async () => {
    cp.view.mockResolvedValue(view([]))
    show()
    expect(await screen.findByText('No runs recorded for this account yet.')).toBeInTheDocument()
  })

  it('shows the detail text for a run that has some', async () => {
    cp.view.mockResolvedValue(view([run({ detail: '3 grants reapplied' })]))
    show()
    expect(await screen.findByText('3 grants reapplied')).toBeInTheDocument()
  })

  it('says when the account could not be read', async () => {
    cp.view.mockRejectedValue(new Error('ledger locked'))
    show()
    expect(await screen.findByText(/ledger locked/)).toBeInTheDocument()
  })

  it('renders each row with a stable test id, newest entries first as the API already sorted them', async () => {
    cp.view.mockResolvedValue(view([run({ jobName: 'seed' }), run({ jobName: 'migrate' })]))
    show()
    const rows = await screen.findAllByTestId(/history-row-/)
    expect(within(rows[0]).getByText('seed')).toBeInTheDocument()
    expect(within(rows[1]).getByText('migrate')).toBeInTheDocument()
  })
})
