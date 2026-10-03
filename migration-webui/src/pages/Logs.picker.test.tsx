import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import Logs from './Logs'

const fetchLogs = vi.fn()
vi.mock('@/api/client', () => ({ fetchLogs: (...a: unknown[]) => fetchLogs(...a) }))
// The page pulls in AiDiagnostics, which imports controlPlane, which reads
// localStorage at module load. Nothing here is testing that panel.
vi.mock('@/components/AiDiagnostics', () => ({ default: () => null }))

/**
 * A launched run's own stdout and stderr go to logs/jobs/{account}/{job}.log.
 * A traceback that kills a migration lands in one of those files and in no
 * other -- the shared engine log never sees it. Until this picker existed,
 * reading one meant logging in to the box.
 */
const payload = (over: Record<string, unknown> = {}) => ({
  path: '/root/migration/migration.log',
  lines: ['engine line one', 'engine line two'],
  jobs: [
    { account: '7', job: 'delta', bytes: 726672, modified: 2 },
    { account: '_none', job: 'seed', bytes: 49643, modified: 1 },
  ],
  ...over,
})

describe('Logs transcript picker', () => {
  beforeEach(() => {
    fetchLogs.mockReset()
    fetchLogs.mockResolvedValue(payload())
  })

  const openPicker = async () => {
    await waitFor(() => expect(screen.getByTestId('log-picker')).toBeTruthy())
    // MUI keeps the options in a portal until the select is opened, so the
    // closed control's text says nothing about what is on offer.
    fireEvent.mouseDown(
      screen.getByTestId('log-picker').querySelector('[role="combobox"]')!)
    return within(await screen.findByRole('listbox'))
  }

  it('offers the job transcripts alongside the engine log', async () => {
    render(<Logs />)
    const menu = await openPicker()
    expect(menu.getByText('migration engine log')).toBeTruthy()
  })

  it('defaults to the engine log, asking for no particular job', async () => {
    render(<Logs />)
    await waitFor(() => expect(fetchLogs).toHaveBeenCalled())
    expect(fetchLogs).toHaveBeenCalledWith('', '')
  })

  it('names each transcript by job, account and size', async () => {
    // Size is the useful discriminator: an empty transcript means the run
    // never got far enough to say anything.
    render(<Logs />)
    const menu = await openPicker()
    expect(menu.getByText(/delta .* account 7 .* 710 KB/)).toBeTruthy()
  })

  it('reads the chosen transcript, not the engine log', async () => {
    render(<Logs />)
    const menu = await openPicker()
    fireEvent.click(menu.getByText(/delta .* account 7/))
    await waitFor(() => expect(fetchLogs).toHaveBeenCalledWith('delta', '7'))
  })

  it('shows no picker when there are no job transcripts', async () => {
    fetchLogs.mockResolvedValue(payload({ jobs: [] }))
    render(<Logs />)
    await waitFor(() => expect(fetchLogs).toHaveBeenCalled())
    expect(screen.queryByTestId('log-picker')).toBeNull()
  })

  it('searches the whole chosen file and shows each match with its context', async () => {
    render(<Logs />)
    const menu = await openPicker()
    fireEvent.click(menu.getByText(/delta .* account 7/))
    fetchLogs.mockResolvedValue(payload({ search: { q: 'PASS', total: 1, matches: [
      { line: 42, before: ['started'], text: 'PASS 1/3 pid=9: drive', after: ['next'] }] } }))
    fireEvent.change(screen.getByTestId('log-search'), { target: { value: 'PASS' } })
    fireEvent.click(screen.getByTestId('log-search-run'))
    await waitFor(() => expect(fetchLogs).toHaveBeenCalledWith('delta', '7', 'PASS'))
    const box = await screen.findByTestId('log-search-results')
    expect(box.textContent).toContain('1 matching line')
    expect(box.textContent).toContain('42: PASS 1/3 pid=9: drive')
  })

  it('survives a server that does not send the list at all', async () => {
    fetchLogs.mockResolvedValue({ path: '/x.log', lines: [] })
    render(<Logs />)
    await waitFor(() => expect(fetchLogs).toHaveBeenCalled())
    expect(screen.queryByTestId('log-picker')).toBeNull()
  })
})
