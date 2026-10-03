import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ProcessPanel from './ProcessPanel'

const fetchProcStats = vi.fn()
const fetchStackDump = vi.fn()
vi.mock('@/api/client', () => ({
  fetchProcStats: (...a: unknown[]) => fetchProcStats(...a),
  fetchStackDump: (...a: unknown[]) => fetchStackDump(...a),
}))
// describeElapsed's module imports controlPlane, which reads localStorage at load.
vi.mock('@/api/controlPlane', () => ({}))

describe('a running job, measured on the box', () => {
  beforeEach(() => {
    fetchProcStats.mockReset(); fetchStackDump.mockReset()
    fetchProcStats.mockResolvedValue({ ok: true, pid: 42, processes: 3, cmd: 'python main.py migrate',
      elapsed_s: 3725, rss_mb: 1368.5, threads: 195, cpu_pct: 110 })
  })

  it('shows memory, CPU, threads and how long, children included', async () => {
    render(<ProcessPanel pid={42} />)
    expect(await screen.findByTestId('process-rss')).toHaveTextContent('1,368.5 MB')
    const panel = screen.getByTestId('process-panel')
    expect(panel).toHaveTextContent('110%')
    expect(panel).toHaveTextContent('195')
    expect(panel).toHaveTextContent('1h 02m')
    expect(panel).toHaveTextContent('processes 3')
    expect(fetchProcStats).toHaveBeenCalledWith(42)
  })

  it('reads where every thread is, on request only', async () => {
    fetchStackDump.mockResolvedValue({ ok: true, dump: 'Thread 1 (idle): run_batch (main.py:900)' })
    render(<ProcessPanel pid={42} />)
    expect(fetchStackDump).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('process-where'))
    await waitFor(() => expect(screen.getByTestId('process-dump'))
      .toHaveTextContent('run_batch (main.py:900)'))
  })

  it("says why when the server will not show it", async () => {
    fetchProcStats.mockResolvedValue({ ok: false, msg: 'superadmin only' })
    render(<ProcessPanel pid={42} />)
    expect(await screen.findByTestId('process-refused')).toHaveTextContent('superadmin only')
  })
})
