/**
 * The graph is a picture of real state: counts come from the ledger's own
 * stage rollups, are shown as counts (never one averaged percentage), and a
 * failed read degrades to the wiring alone rather than to invented numbers.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Pipeline from './Pipeline'
import * as client from '@/api/client'
import * as cp from '@/api/controlPlane'

vi.mock('@/api/client', () => ({ fetchStages: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({ fetchFleet: vi.fn() }))

const stage = (id: string, status: string, done: number) => ({
  id, name: id, description: '', status, progress: 0,
  usersCompleted: done, usersTotal: 300, expanded: false,
})

beforeEach(() => {
  vi.mocked(cp.fetchFleet).mockResolvedValue([])
})

describe('Pipeline', () => {
  it('puts the ledger\'s per-stage counts on the nodes, as counts', async () => {
    vi.mocked(client.fetchStages).mockResolvedValue([
      stage('drive', 'in_progress', 12), stage('gmail', 'waiting', 0),
      stage('validation', 'waiting', 0),
    ] as never)
    render(<Pipeline />)
    expect(await screen.findByText('running 12/300 users')).toBeInTheDocument()
    expect(screen.getByText('waiting 0/300 users')).toBeInTheDocument()
    // The wire out of a running stage pulses; the others do not.
    expect(document.querySelectorAll('.pulse').length).toBeGreaterThan(0)
    expect(screen.getByTestId('node-drive').textContent).not.toMatch(/%/)
  })

  it('says which file does each job', async () => {
    vi.mocked(client.fetchStages).mockResolvedValue([] as never)
    render(<Pipeline />)
    expect(await screen.findByText('drive_engine.py')).toBeInTheDocument()
    expect(screen.getByText('resilience.py · rate/retry')).toBeInTheDocument()
  })

  it('shows the wiring alone, with a notice, when the ledger cannot be read', async () => {
    vi.mocked(client.fetchStages).mockRejectedValue(new Error('no database yet'))
    render(<Pipeline />)
    expect(await screen.findByText(/Live counts unavailable \(no database yet\)/)).toBeInTheDocument()
    expect(screen.getByTestId('node-drive')).toBeInTheDocument()
    await waitFor(() => expect(document.querySelectorAll('.pulse').length).toBe(0))
    expect(screen.queryByText(/users$/)).toBeNull()
  })

  it('reports an untracked stage as untracked instead of guessing', async () => {
    vi.mocked(client.fetchStages).mockResolvedValue([stage('user_creation', 'not_started', 0)] as never)
    render(<Pipeline />)
    expect(await screen.findByText('not tracked')).toBeInTheDocument()
  })
})
