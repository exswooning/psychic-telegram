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

  it('crowns the fastest CLEAN arm and still lists a faster arm that was not clean', async () => {
    fetchAb.mockResolvedValue({ running: false, results: { runs: [] }, progress: progress({ alive: false, done: 6 }),
      ranking: [
        { arm: 'server_side + split', seconds: 60, failed: 0, clean: true, verdicts: {} },
        { arm: 'server_side + engine', seconds: 40, failed: 3, clean: false, verdicts: { 'audit/gmail': 'DIFFERENCES' } },
      ] })
    render(<AbBenchmarkPanel />)
    await waitFor(() => expect(screen.getByTestId('ab-best')).toHaveTextContent('server_side + split'))
    expect(screen.getByTestId('ab-best')).not.toHaveTextContent('server_side + engine')
    expect(screen.getByText('not clean')).toBeTruthy()
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
