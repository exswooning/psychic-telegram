/**
 * Trimming filler is a delete, so what matters is the order it forces:
 * type the domain, preview, read what it would remove, and only then delete.
 * And that everything shown is what the job's log said, not what was assumed.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import SeedTrimFiller from './SeedTrimFiller'

const api = vi.hoisted(() => ({ fetchTrimStatus: vi.fn(), trimFiller: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({ fetchTrimStatus: api.fetchTrimStatus, trimFiller: api.trimFiller }))

const status = (over = {}) => ({
  hasRun: true, running: false, mode: 'preview', done: 300, total: 300,
  affected: ['[george@src.example] 233.1GB vs share 30.0GB: would delete 4,062 filler file(s) (203.1GB)'],
  summary: '12 account(s) over their share. Would delete 48,700 filler file(s), 2,400.0 GB.', lines: [], ...over,
})
const none = { hasRun: false, running: false, mode: null, done: 0, total: 0, affected: [], summary: null, lines: [] }

// The status poll resolves after mount; let it land before asserting.
const settle = () => act(async () => { await Promise.resolve() })
const type = () => fireEvent.change(screen.getByTestId('trim-domain'), { target: { value: 'src.example' } })

beforeEach(() => {
  api.fetchTrimStatus.mockReset().mockResolvedValue(none)
  api.trimFiller.mockReset().mockResolvedValue({ ok: true, actionId: 1, detail: 'started pid 9' })
})

describe('SeedTrimFiller', () => {
  it('will not do anything until the domain is typed', async () => {
    render(<SeedTrimFiller domain="src.example" />)
    await settle()
    expect(screen.getByRole('button', { name: 'Preview' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete filler' })).toBeDisabled()
  })

  it('previews by default: the first press never sends apply', async () => {
    render(<SeedTrimFiller domain="src.example" accountId={2} />)
    type()
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => expect(api.trimFiller).toHaveBeenCalledWith('src.example', { apply: false, accountId: 2 }))
  })

  it('will not offer the delete before a preview has finished, and says why', async () => {
    render(<SeedTrimFiller domain="src.example" />)
    await settle()
    type()
    expect(screen.getByRole('button', { name: 'Delete filler' })).toBeDisabled()
    expect(screen.getByTestId('trim-needs-preview')).toBeInTheDocument()
  })

  it('unlocks the delete once a preview has run to its summary', async () => {
    api.fetchTrimStatus.mockResolvedValue(status())
    render(<SeedTrimFiller domain="src.example" accountId={2} />)
    type()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete filler' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Delete filler' }))
    await waitFor(() => expect(api.trimFiller).toHaveBeenCalledWith('src.example', { apply: true, accountId: 2 }))
  })

  it('does not unlock the delete while the preview is still running', async () => {
    api.fetchTrimStatus.mockResolvedValue(status({ running: true, summary: null, done: 40 }))
    render(<SeedTrimFiller domain="src.example" />)
    type()
    await screen.findByTestId('trim-status')
    expect(screen.getByRole('button', { name: 'Delete filler' })).toBeDisabled()
  })

  it('does not unlock the delete on the strength of an earlier delete', async () => {
    api.fetchTrimStatus.mockResolvedValue(status({ mode: 'apply' }))
    render(<SeedTrimFiller domain="src.example" />)
    type()
    await screen.findByTestId('trim-status')
    expect(screen.getByRole('button', { name: 'Delete filler' })).toBeDisabled()
  })

  it('shows what a preview found, and that nothing was deleted', async () => {
    api.fetchTrimStatus.mockResolvedValue(status())
    render(<SeedTrimFiller domain="src.example" />)
    expect(await screen.findByTestId('trim-summary')).toHaveTextContent('Would delete 48,700')
    expect(screen.getByTestId('trim-affected')).toHaveTextContent('george@src.example')
    expect(screen.getByText(/nothing was deleted/)).toBeInTheDocument()
  })

  it('shows a real progress count while running, and 0 accounts as 0, not as no data', async () => {
    api.fetchTrimStatus.mockResolvedValue(status({ running: true, summary: null, done: 0, total: 300, affected: [] }))
    render(<SeedTrimFiller domain="src.example" />)
    expect(await screen.findByTestId('trim-status')).toHaveTextContent('0 of 300 accounts checked')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0')
  })

  it('says nothing about a run when there has not been one', async () => {
    render(<SeedTrimFiller domain="src.example" />)
    await waitFor(() => expect(api.fetchTrimStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('trim-status')).toBeNull()
  })

  it('shows why a start was refused', async () => {
    api.trimFiller.mockResolvedValue({ ok: false, actionId: 1, detail: 'src.example is protected' })
    render(<SeedTrimFiller domain="src.example" />)
    type()
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText('src.example is protected')).toBeInTheDocument()
  })
})
