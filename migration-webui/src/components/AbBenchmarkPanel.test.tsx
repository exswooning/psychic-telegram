import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const fetchAb = vi.fn()
vi.mock('@/api/controlPlane', () => ({ fetchAbBenchmark: (...a: unknown[]) => fetchAb(...a) }))

import AbBenchmarkPanel from './AbBenchmarkPanel'
import BenchmarkPill from './BenchmarkPill'

const progress = (over = {}) => ({
  startedAt: 't', current: { mode: 'server_side', mailMode: 'split', stage: 'migrate' },
  users: ['audit@s.example.com'], corpus: { files: 106, bytes: 176e6 }, total: 6, done: 0,
  alive: true, armsFinished: 0, meanArmSeconds: null, etaSeconds: null,
  etaNote: 'no estimate until one arm has finished', ...over,
})

describe('A/B benchmark page', () => {
  beforeEach(() => fetchAb.mockReset())

  it('says there is no estimate until one arm has finished, and shows no number', async () => {
    fetchAb.mockResolvedValue({ running: true, progress: progress(), results: null, ranking: [] })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getByTestId('ab-eta-note')).toBeTruthy())
    expect(screen.queryByTestId('ab-eta')).toBeNull()
    expect(screen.getByTestId('ab-progress')).toHaveTextContent('Arm 1 of 6')
    expect(screen.getByTestId('ab-progress')).toHaveTextContent('server_side + split · migrate')
  })

  it('shows the time left once arms have finished, with what it was computed from', async () => {
    fetchAb.mockResolvedValue({ running: true, results: null, ranking: [],
      progress: progress({ done: 2, armsFinished: 2, meanArmSeconds: 400, etaSeconds: 1500, etaNote: null }) })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getByTestId('ab-eta')).toHaveTextContent('about 25m 0s left'))
    expect(screen.getByTestId('ab-eta')).toHaveTextContent('from 2 finished')
  })

  it('names the default: fewest errors, then fastest; an unranked arm says why and never wins', async () => {
    fetchAb.mockResolvedValue({ running: false, results: { runs: [] }, progress: progress({ alive: false, done: 4 }),
      best: { arm: 'server_side + engine', mode: 'server_side', mailMode: 'engine', errors: 4, seconds: 1600 },
      ranking: [
        { arm: 'server_side + engine', seconds: 1600, failed: 0, clean: false, errors: 4, judged: true,
          verdicts: { 'a/chat': 'DIFFERENCES' } },
        { arm: 'server_side + split', seconds: 300, failed: 0, clean: false, errors: null, judged: false,
          why: "Google's mail import: skipped", verdicts: {} },
      ] })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getByTestId('ab-best')).toHaveTextContent('Default for new runs: server_side + engine'))
    expect(screen.getByTestId('ab-best')).toHaveTextContent('fewest errors (4)')
    expect(screen.getAllByTestId('ab-errors')[0]).toHaveTextContent('4')
    expect(screen.getAllByTestId('ab-errors')[1]).toHaveTextContent("not ranked: Google's mail import: skipped")
    expect(screen.getByText('default')).toBeTruthy()
  })

  it('lists every arm running side by side, the skipped slow arm, and how each time was taken', async () => {
    fetchAb.mockResolvedValue({ running: true, results: null, best: null,
      progress: progress({ phase: 'side by side', skipped: ['download_upload + engine'],
        arms: [{ arm: 'server_side + engine', stage: 'migrate' }, { arm: 'server_side + split', stage: 'verify 1:1' }] }),
      ranking: [{ arm: 'server_side + split', seconds: 300, failed: 0, clean: false, errors: 2, judged: true,
        timing: 'side by side', verdicts: {} }] })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getByTestId('ab-arms')).toHaveTextContent('server_side + engine · migrate; server_side + split · verify 1:1'))
    expect(screen.getByTestId('ab-skipped')).toHaveTextContent('download_upload + engine')
    expect(screen.getByText('shared the box')).toBeTruthy()
  })

  it('says new runs keep the configured defaults while no arm qualifies', async () => {
    fetchAb.mockResolvedValue({ running: false, results: { runs: [] }, progress: progress({ alive: false, done: 1 }),
      best: null, ranking: [{ arm: 'server_side + split', seconds: 300, failed: 0, clean: false,
        errors: null, judged: false, why: 'its 1:1 check did not run', verdicts: {} }] })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getByTestId('ab-no-best')).toHaveTextContent('keep the configured defaults'))
    expect(screen.queryByTestId('ab-best')).toBeNull()
  })

  it('shows what differed and the mail-import status in the row, not behind a hover', async () => {
    fetchAb.mockResolvedValue({ running: false, results: null, progress: progress({ alive: false, done: 4 }),
      ranking: [
        { arm: 'server_side + engine', seconds: 1602, failed: 0, clean: false, dms: null,
          verdicts: { 'audit/gmail': 'DIFFERENCES', 'audit/drive': 'IDENTICAL' },
          differences: { 'audit/gmail': { counts: { missing: 756, extras: 1451 } } } },
        { arm: 'server_side + split', seconds: 261, failed: 5, clean: false,
          dms: 'skipped: no Admin console login for admin@t.example.com',
          verdicts: { 'audit/gmail': 'DIFFERENCES' } },
      ] })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getAllByTestId('ab-differences')[0]).toHaveTextContent('audit/gmail DIFFERENCES — 756 missing, 1451 extras'))
    expect(screen.getAllByTestId('ab-differences')[0]).not.toHaveTextContent('audit/drive')
    expect(screen.getAllByTestId('ab-differences')[1]).toHaveTextContent('5 item(s) failed')
    expect(screen.getAllByTestId('ab-dms')[1]).toHaveTextContent('skipped: no Admin console login')
    expect(screen.getAllByTestId('ab-dms')[0]).toHaveTextContent('not run')
  })

  it('groups what differed by kind, saying which kinds fix themselves', async () => {
    fetchAb.mockResolvedValue({ running: false, results: null, best: null, progress: progress({ alive: false, done: 1 }),
      ranking: [{ arm: 'server_side + engine', seconds: 1600, failed: 0, clean: false, errors: 4, judged: true,
        verdicts: { 'a/chat': 'DIFFERENCES' },
        differences: { 'a/chat': { counts: { differences: 4 }, kinds: [
          { kind: 'chat.attributed', count: 1, title: 'Chat message re-posted with an attribution', auto: false },
          { kind: 'chat.labels', count: 2, title: 'A label did not come across', auto: true }] } } }] })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getAllByTestId('ab-kind')).toHaveLength(2))
    expect(screen.getAllByTestId('ab-kind')[0]).toHaveTextContent('1 × Chat message re-posted with an attribution (needs a person or cannot be fixed)')
    expect(screen.getAllByTestId('ab-kind')[1]).toHaveTextContent('(fixed automatically)')
  })

  it('the header pill appears only while a run is going', async () => {
    fetchAb.mockResolvedValue({ running: false, progress: progress({ alive: false }), results: null, ranking: [] })
    const { rerender } = render(<MemoryRouter><BenchmarkPill /></MemoryRouter>)
    await waitFor(() => expect(fetchAb).toHaveBeenCalled())
    expect(screen.queryByTestId('benchmark-pill')).toBeNull()
    fetchAb.mockResolvedValue({ running: true, progress: progress({ etaSeconds: 600, etaNote: null }), results: null, ranking: [] })
    rerender(<MemoryRouter><BenchmarkPill key="again" /></MemoryRouter>)
    await waitFor(() => expect(screen.getByTestId('benchmark-pill')).toHaveTextContent('Benchmark 1/6'))
    expect(screen.getByTestId('benchmark-pill')).toHaveTextContent('~10m 0s left')
  })
})

describe('A/B benchmark page, a run that failed', () => {
  it('shows why it stopped instead of reading as 0 of N', async () => {
    fetchAb.mockResolvedValue({ running: false, results: null, ranking: [],
      progress: { ...progress({ alive: false, done: 0 }), error: 'RuntimeError: target reset refused: t.example is protected' } })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getByTestId('ab-error')).toHaveTextContent('t.example is protected'))
  })
})
