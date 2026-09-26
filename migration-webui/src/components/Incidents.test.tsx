/**
 * The incidents panel is what turns "something broke overnight" into "paste
 * this into Claude Code". What matters: nothing is hidden when the list is
 * empty or the server errors, an open problem reads as one, and a brief that
 * cannot be copied is shown rather than lost.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Incidents from './Incidents'

const api = vi.hoisted(() => ({ fetchIncidents: vi.fn(), setIncidentStatus: vi.fn(), fetchIncidentBrief: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchIncidents: api.fetchIncidents, setIncidentStatus: api.setIncidentStatus,
  fetchIncidentBrief: api.fetchIncidentBrief,
}))

const inc = (over = {}) => ({
  id: 7, account_id: 3, job_name: 'migrate', kind: 'crashed', severity: 'error',
  title: 'migrate exited with signal 6', summary: 'ended with exit signal 6', run_id: 'migration-1',
  occurrences: 1, status: 'open', note: null, opened_at: '2026-09-26T01:00:00Z',
  last_seen_at: '2026-09-26T01:00:00Z', resolved_at: null, ...over,
})

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  api.fetchIncidents.mockResolvedValue({ incidents: [] })
  api.setIncidentStatus.mockResolvedValue({ ok: true })
})

describe('Incidents', () => {
  it('says plainly that nothing is recorded, rather than drawing an empty box', async () => {
    render(<Incidents />)
    expect(await screen.findByTestId('no-incidents')).toBeInTheDocument()
  })

  it('shows an open incident as its severity, and counts the open ones', async () => {
    api.fetchIncidents.mockResolvedValue({ incidents: [inc(), inc({ id: 8, status: 'acknowledged' })] })
    render(<Incidents />)
    expect(await screen.findByTestId('incident-status-7')).toHaveTextContent('error')
    expect(screen.getByTestId('incident-status-8')).toHaveTextContent('acknowledged')
    expect(screen.getByTestId('incidents-open')).toHaveTextContent('1 open')
  })

  it('hides resolved incidents until asked', async () => {
    api.fetchIncidents.mockResolvedValue({ incidents: [inc({ id: 9, status: 'resolved' })] })
    render(<Incidents />)
    await screen.findByTestId('no-incidents')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Show resolved' }))
    expect(await screen.findByTestId('incident-9')).toBeInTheDocument()
  })

  it('says how many times a recurring problem has been seen', async () => {
    api.fetchIncidents.mockResolvedValue({ incidents: [inc({ occurrences: 4 })] })
    render(<Incidents />)
    expect(await screen.findByTestId('incident-7')).toHaveTextContent('seen 4 times')
  })

  it('acknowledges and resolves through the API, then reloads', async () => {
    api.fetchIncidents.mockResolvedValue({ incidents: [inc()] })
    render(<Incidents />)
    fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge' }))
    await waitFor(() => expect(api.setIncidentStatus).toHaveBeenCalledWith(7, 'acknowledged'))
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    await waitFor(() => expect(api.setIncidentStatus).toHaveBeenCalledWith(7, 'resolved'))
    expect(api.fetchIncidents.mock.calls.length).toBeGreaterThan(2)
  })

  it('copies the brief to the clipboard', async () => {
    api.fetchIncidents.mockResolvedValue({ incidents: [inc()] })
    api.fetchIncidentBrief.mockResolvedValue('# brief\nsteps')
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    render(<Incidents />)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy brief' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('# brief\nsteps'))
    expect(await screen.findByText(/Brief copied/)).toBeInTheDocument()
  })

  it('shows the brief instead when the browser refuses to copy it', async () => {
    api.fetchIncidents.mockResolvedValue({ incidents: [inc()] })
    api.fetchIncidentBrief.mockResolvedValue('# brief\nsteps')
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    render(<Incidents />)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy brief' }))
    expect(await screen.findByTestId('brief-text')).toHaveTextContent('# brief')
  })

  it('reports a failure to load instead of pretending there are none', async () => {
    api.fetchIncidents.mockRejectedValue(new Error('HTTP 500'))
    render(<Incidents />)
    expect(await screen.findByText('HTTP 500')).toBeInTheDocument()
  })
})
