/**
 * The Mirror page: a pair that never cycled has an Unknown lag (never a green one), a
 * pair behind says so, the last cycle's changes are counts by kind and service, held
 * deletions wait for a person, and each write asks for a reason first.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Mirror from './Mirror'
import { duration } from '@/mirrorKinds'
import type { MirrorCycle, MirrorView } from '@/api/controlPlane'

const cp = vi.hoisted(() => ({ view: vi.fn(), save: vi.fn(), run: vi.fn(), decide: vi.fn(), migs: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 3 }),
  fetchMirror: cp.view,
  saveMirrorSettings: cp.save,
  runMirrorCycle: cp.run,
  decideMirrorDeletions: cp.decide,
  fetchMirrorMigrations: cp.migs,
}))

const settings = { enabled: true, intervalMin: 15, deletionMode: 'mirror' as const, capPct: 2,
  deletionsPaused: false, enabledAt: '2026-09-30T10:00:00Z', updatedBy: 'op', updatedAt: null }
const cycle = (over: Partial<MirrorCycle> = {}): MirrorCycle => ({
  id: 7, startedAt: '2026-09-30T10:00:00Z', finishedAt: '2026-09-30T10:02:00Z', status: 'ok',
  calls: 412, counts: { edited: 2, renamed: 1, new: 3 },
  byService: { drive: { edited: 2, renamed: 1, new: 1 }, gmail: { new: 2, labels: 4 } },
  errors: [], unknown: [], users: {}, deletionsProposed: 0, deletionsApplied: 0, deletionsHeld: 0,
  conflicts: 0, ...over,
})
const view = (over: Partial<MirrorView> = {}): MirrorView => ({
  accountId: 3, sourceDomain: 'source.example.com', targetDomain: 'target.example.com',
  settings, minIntervalMin: 5, lagIntervals: 3, running: false, cycles: [cycle()], lastCycle: cycle(),
  lastGoodAt: '2026-09-30T10:02:00Z', lagSeconds: 300, behind: false,
  waiting: { retry: 0, held: 0 }, held: [], conflicts: [], conflictCount: 0,
  cannotMirror: ['Drive revision history'], ...over,
})
const show = () => render(<MemoryRouter initialEntries={['/mirror?account=3']}><Mirror /></MemoryRouter>)
const confirm = async (reason = 'because the owners asked') => {
  fireEvent.change(await screen.findByLabelText('Reason Code'), { target: { value: reason } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
}

beforeEach(() => {
  cp.migs.mockReset().mockResolvedValue({ accountId: 3, migrations: [
    { id: 41, startedAt: '2026-10-03T12:13:00Z', reason: 'chat for the rehearsal', users: ['seeduser200@src.example'] }] })
  cp.view.mockReset().mockResolvedValue(view())
  for (const f of [cp.save, cp.run, cp.decide]) f.mockReset().mockResolvedValue({ ok: true, actionId: 1, detail: 'started pid 9' })
})

describe('the state of the pair', () => {
  it('reads a lag nobody could measure as Unknown, never as fine', async () => {
    cp.view.mockResolvedValue(view({ settings: { ...settings, enabled: false }, cycles: [], lastCycle: null,
      lastGoodAt: null, lagSeconds: null, behind: null }))
    show()
    expect(await screen.findByTestId('lag')).toHaveTextContent('Unknown')
    expect(screen.getByTestId('last-status')).toHaveTextContent('none yet')
    expect(screen.getByTestId('mirror-on')).toHaveTextContent('Off')
  })

  it('says when the pair is behind', async () => {
    cp.view.mockResolvedValue(view({ lagSeconds: 3 * 3600, behind: true }))
    show()
    expect(await screen.findByTestId('lag')).toHaveTextContent('3 h 0 min — behind')
  })

  it('shows the last cycle as counts by kind and service', async () => {
    show()
    const drive = await screen.findByTestId('kinds-drive')
    expect(drive).toHaveTextContent('drive')
    const gmail = screen.getByTestId('kinds-gmail')
    expect(gmail.textContent).toContain('4')        // labels
    // counts only: no blended percentage in the state or the changes
    expect(screen.getByTestId('kinds').textContent).not.toContain('%')
    expect(screen.getByTestId('mirror-state').textContent).not.toContain('%')
  })

  it('lists what a cycle could not check, in amber', async () => {
    cp.view.mockResolvedValue(view({ lastCycle: cycle({ unknown: ['source users could not be listed'] }) }))
    show()
    expect(await screen.findByTestId('unknown')).toHaveTextContent('source users could not be listed')
  })

  it('will not start a second cycle while one runs', async () => {
    cp.view.mockResolvedValue(view({ running: true }))
    show()
    expect(await screen.findByTestId('mirror-run')).toBeDisabled()
  })
})

describe('held deletions', () => {
  const heldView = view({
    waiting: { retry: 0, held: 2 },
    settings: { ...settings, deletionsPaused: true },
    held: [
      { id: 1, sourceUser: 'ann@a.com', service: 'drive', itemType: 'file', name: 'q3.pdf', targetId: 't1', detail: null, createdAt: '2026-09-30T10:00:00Z' },
      { id: 2, sourceUser: 'ann@a.com', service: 'gmail', itemType: 'message', name: null, targetId: 't2', detail: null, createdAt: '2026-09-30T10:00:00Z' },
    ],
  })

  it('shows them, and applies them only after a reason', async () => {
    cp.view.mockResolvedValue(heldView)
    show()
    expect(await screen.findByTestId('held')).toHaveTextContent('2 deletion(s) are waiting')
    expect(screen.getByText('q3.pdf')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('apply-deletions'))
    expect(cp.decide).not.toHaveBeenCalled()
    await confirm()
    await waitFor(() => expect(cp.decide).toHaveBeenCalledWith('because the owners asked', 'apply', 3))
  })

  it('keeps them', async () => {
    cp.view.mockResolvedValue(heldView)
    show()
    fireEvent.click(await screen.findByTestId('keep-deletions'))
    await confirm()
    await waitFor(() => expect(cp.decide).toHaveBeenCalledWith('because the owners asked', 'keep', 3))
  })

  it('shows nothing to decide when nothing is held', async () => {
    show()
    await screen.findByTestId('kinds')
    expect(screen.queryByTestId('held')).toBeNull()
  })
})

describe('settings', () => {
  it('refuses an interval under the minimum before anything is sent', async () => {
    show()
    fireEvent.change(await screen.findByTestId('mirror-interval'), { target: { value: '4' } })
    expect(screen.getByTestId('mirror-save')).toBeDisabled()
    expect(screen.getByText('At least 5 minutes')).toBeInTheDocument()
  })

  it('saves the switch, interval, mode and cap together', async () => {
    show()
    fireEvent.change(await screen.findByTestId('mirror-interval'), { target: { value: '30' } })
    fireEvent.click(screen.getByLabelText(/Keep everything/))
    fireEvent.click(screen.getByTestId('mirror-save'))
    await confirm('slower at night')
    await waitFor(() => expect(cp.save).toHaveBeenCalledWith('slower at night',
      { enabled: true, intervalMin: 30, deletionMode: 'keep', capPct: 2, users: null }, 3))
  })

  it('runs a cycle now after a reason', async () => {
    show()
    fireEvent.click(await screen.findByTestId('mirror-run'))
    await confirm('checking a fix')
    await waitFor(() => expect(cp.run).toHaveBeenCalledWith('checking a fix', 3))
  })
})

describe('duration', () => {
  it('reads like a person would say it', () => {
    expect(duration(45)).toBe('45 s')
    expect(duration(600)).toBe('10 min')
    expect(duration(3 * 3600 + 120)).toBe('3 h 2 min')
    expect(duration(3 * 86400)).toBe('3 d 0 h')
  })
})


describe('a mirror can follow one migration', () => {
  it('saves the chosen migration\'s users with the settings', async () => {
    show()
    const select = await screen.findByTestId('mirror-follows')
    fireEvent.mouseDown(select.parentElement!.querySelector('[role="combobox"]')!)
    fireEvent.click(await screen.findByTestId('mirror-migration-41'))
    fireEvent.click(screen.getByTestId('mirror-save'))
    await confirm('follow the rehearsal')
    await waitFor(() => expect(cp.save).toHaveBeenCalled())
    expect(cp.save.mock.calls[0][1].users).toEqual(['seeduser200@src.example'])
  })
})
