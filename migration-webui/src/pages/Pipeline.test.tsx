/**
 * The picture of the whole system. What is tested is what can mislead: a box
 * showing a number nothing measured, a description that is missing, a click
 * that lights the wrong thing, and a failed read that turns into invented
 * figures instead of the plain wiring.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Pipeline from './Pipeline'

const api = vi.hoisted(() => ({
  fetchStages: vi.fn(), fetchQueue: vi.fn(), fetchFleet: vi.fn(), fetchDeadman: vi.fn(), fetchMyMetrics: vi.fn(),
}))
vi.mock('@/api/client', () => ({ fetchStages: api.fetchStages, fetchQueue: api.fetchQueue }))
vi.mock('@/api/controlPlane', () => ({
  fetchFleet: api.fetchFleet, fetchDeadman: api.fetchDeadman, fetchMyMetrics: api.fetchMyMetrics,
}))

const stage = (id: string, status: string, done: number) => ({
  id, name: id, description: '', status, progress: 0,
  usersCompleted: done, usersTotal: 300, expanded: false,
})
const view = () => render(<MemoryRouter><Pipeline /></MemoryRouter>)

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  api.fetchStages.mockResolvedValue([])
  api.fetchQueue.mockResolvedValue({ capacity: 2, running: [], waiting: [], recent: [] })
  api.fetchFleet.mockResolvedValue([])
  api.fetchDeadman.mockResolvedValue({ armed: false, secondsRemaining: null })
  api.fetchMyMetrics.mockResolvedValue({ error: 'no metrics recorded yet' })
})

describe('Pipeline', () => {
  it('draws every part, in sections, and says how many', async () => {
    view()
    expect(await screen.findByText(/65 parts in 13 sections/)).toBeInTheDocument()
    for (const id of ['src', 'wizard', 'drive', 'gmail', 'lim_tgt', 'id_mapping', 'tgt', 'undo', 'mc'])
      expect(screen.getByTestId(`node-${id}`)).toBeInTheDocument()
    for (const f of ['setup', 'engines', 'ledger', 'verify']) expect(screen.getByTestId(`frame-${f}`)).toBeInTheDocument()
  })

  it('puts the ledger\'s counts on the engines, as counts', async () => {
    api.fetchStages.mockResolvedValue([stage('drive', 'in_progress', 129), stage('gmail', 'waiting', 0)])
    view()
    await waitFor(() => expect(screen.getByTestId('node-drive').textContent).toContain('running 129/300 users'))
    expect(screen.getByTestId('node-gmail').textContent).toContain('waiting 0/300 users')
    expect(screen.getByTestId('node-drive').textContent).not.toMatch(/%/)
  })

  it('shows no reading on a part that nothing measures', async () => {
    api.fetchStages.mockResolvedValue([stage('drive', 'in_progress', 1)])
    view()
    await waitFor(() => expect(screen.getByTestId('node-drive').textContent).toContain('running'))
    // Tasks has no stage, no metric and no queue: its box carries only its own name and file.
    expect(screen.getByTestId('node-tasks').textContent).toBe('Tasks — tasks_engine.py' + 'Tasks' + 'tasks_engine.py' + 'User turn' + 'Reads' + 'Writes' + 'Rows' + 'Mappings')
  })

  it('degrades to the plain wiring, with a notice, when the ledger cannot be read', async () => {
    api.fetchStages.mockRejectedValue(new Error('no database yet'))
    view()
    expect(await screen.findByText(/Live counts unavailable \(no database yet\)/)).toBeInTheDocument()
    expect(screen.getByTestId('node-drive')).toBeInTheDocument()
  })

  it('labels metrics from an ended run as idle, not current', async () => {
    api.fetchMyMetrics.mockResolvedValue({
      error: '', operations: [], history: [],
      latest: { recordedAt: '2026-09-01T00:00:00Z', requestsPerSec: 66.4, p95: 1.7, workers: 45 },
      limiters: { target: { rate: 45, floor: 5, ceiling: 1200, rejections: 9, backoffs: 6 } },
    })
    view()
    await waitFor(() => expect(screen.getByTestId('node-lim_tgt').textContent).toContain('idle · last 45/s'))
    expect(screen.getByTestId('node-lim_tgt').textContent).not.toContain('6 pushbacks')
  })

  describe('selecting a part', () => {
    it('explains it: what it is, where it runs, its files, and its neighbours', async () => {
      view()
      fireEvent.click(await screen.findByTestId('node-gmail'))
      const d = within(screen.getByTestId('pipeline-selected'))
      expect(d.getByText('Gmail')).toBeInTheDocument()
      expect(d.getByText(/messages\.insert, not import/)).toBeInTheDocument()
      expect(d.getByText('Where it lives')).toBeInTheDocument()
      expect(d.getAllByText('gmail_engine.py')).toHaveLength(2)      // subtitle, and the file list
      expect(d.getByText(/runs on: Any machine/)).toBeInTheDocument()
      expect(d.getByText(/Source tenant · Reads/)).toBeInTheDocument()       // comes from
      expect(d.getByText(/Link rewrite · Messages/)).toBeInTheDocument()     // feeds
    })

    it('says plainly when nothing is measuring the part live', async () => {
      view()
      fireEvent.click(await screen.findByTestId('node-tasks'))
      expect(screen.getByText('Nothing measures this part live.')).toBeInTheDocument()
    })

    it('follows a neighbour link to the next part', async () => {
      view()
      fireEvent.click(await screen.findByTestId('node-gmail'))
      fireEvent.click(screen.getByText(/Link rewrite · Messages/))
      expect(within(screen.getByTestId('pipeline-selected')).getByText('Link rewrite')).toBeInTheDocument()
    })

    it('dims everything not upstream or downstream of it', async () => {
      view()
      fireEvent.click(await screen.findByTestId('node-gmail'))
      expect(screen.getByTestId('node-gmail')).toHaveAttribute('opacity', '1')
      expect(screen.getByTestId('node-src')).toHaveAttribute('opacity', '1')       // upstream
      expect(screen.getByTestId('node-wizard')).toHaveAttribute('opacity', '1')    // upstream, through delegation and keys
      expect(screen.getByTestId('node-deadman')).toHaveAttribute('opacity', '0.2') // connected to nothing on Gmail's path
    })

    it('clears with Escape', async () => {
      view()
      fireEvent.click(await screen.findByTestId('node-gmail'))
      expect(screen.getByTestId('pipeline-selected')).toBeInTheDocument()
      fireEvent.keyDown(window, { key: 'Escape' })
      await waitFor(() => expect(screen.queryByTestId('pipeline-selected')).toBeNull())
    })
  })

  describe('getting around', () => {
    it('offers a jump to every section, and to the whole', async () => {
      view()
      const bar = within(await screen.findByTestId('pipeline-jump'))
      for (const t of ['Setup & access', 'Engines', 'Ledger', 'Verify & repair', 'Watch', 'Everything'])
        expect(bar.getByText(t)).toBeInTheDocument()
    })

    it('offers to zoom to the selected part\'s path', async () => {
      view()
      fireEvent.click(await screen.findByTestId('node-drive'))
      expect(screen.getByRole('button', { name: 'Zoom to its path' })).toBeInTheDocument()
    })
  })

  describe('finding a part', () => {
    it('matches by name, by file and by what it does, and counts the matches', async () => {
      view()
      const box = await screen.findByTestId('pipeline-search')
      fireEvent.change(box, { target: { value: 'resilience.py' } })
      const found = ['retry', 'quota', 'lim_src', 'lim_tgt']
      for (const id of found) expect(screen.getByTestId(`node-${id}`)).toHaveAttribute('opacity', '1')
      expect(screen.getByTestId('node-drive')).toHaveAttribute('opacity', '0.2')
      expect(screen.getByTestId('pipeline-matches')).toHaveTextContent(/\d+ matches/)
      fireEvent.change(box, { target: { value: '3/sec/account' } })
      expect(screen.getByTestId('node-gmail')).toHaveAttribute('opacity', '1')
      expect(screen.getByTestId('pipeline-matches')).toHaveTextContent('1 match')
    })

    it('lights nothing up for a search that finds nothing, rather than hiding everything silently', async () => {
      view()
      fireEvent.change(await screen.findByTestId('pipeline-search'), { target: { value: 'zzzz-no-such-thing' } })
      expect(screen.getByTestId('pipeline-matches')).toHaveTextContent('0 matches')
    })
  })
})
