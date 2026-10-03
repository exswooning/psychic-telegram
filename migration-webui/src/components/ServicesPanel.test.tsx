import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ServicesPanel from './ServicesPanel'

const fetchHostServices = vi.fn()
const restartHostUnit = vi.fn()
vi.mock('@/api/client', () => ({
  fetchHostServices: (...a: unknown[]) => fetchHostServices(...a),
  restartHostUnit: (...a: unknown[]) => restartHostUnit(...a),
}))

const unit = (name: string, active = 'active') => ({
  unit: name, active, sub: 'running', since: 'Sat 2026-10-03 09:16', restarts: 0, recent: [] as string[] })

describe('the services, without SSH', () => {
  beforeEach(() => { fetchHostServices.mockReset(); restartHostUnit.mockReset() })

  it('shows each unit, the deployed commit and any OOM kill', async () => {
    fetchHostServices.mockResolvedValue({ ok: true, deployed_commit: '068994f', busy: [],
      units: [unit('bitport-api'), { ...unit('bitport-webui', 'failed'), recent: ['boom'] }],
      oom: ['kernel: Out of memory: Killed process 42 (python)'] })
    render(<ServicesPanel />)
    expect(await screen.findByTestId('deployed-commit')).toHaveTextContent('068994f')
    expect(screen.getByTestId('unit-bitport-webui')).toHaveTextContent('failed')
    expect(screen.getByTestId('unit-bitport-webui')).toHaveTextContent('boom')
    expect(screen.getByTestId('oom-kills')).toHaveTextContent('Killed process 42')
  })

  it('will not restart while a job runs', async () => {
    fetchHostServices.mockResolvedValue({ ok: true, deployed_commit: 'x', oom: [],
      busy: ['migrate (account 3, pid 9)'], units: [unit('bitport-api')] })
    render(<ServicesPanel />)
    expect(await screen.findByTestId('restart-bitport-api')).toBeDisabled()
    expect(screen.getByTestId('services-panel')).toHaveTextContent('migrate (account 3, pid 9)')
  })

  it('restarts after a confirmation when idle', async () => {
    fetchHostServices.mockResolvedValue({ ok: true, deployed_commit: 'x', oom: [], busy: [],
      units: [unit('bitport-api')] })
    restartHostUnit.mockResolvedValue({ ok: true, msg: 'bitport-api is restarting' })
    render(<ServicesPanel />)
    fireEvent.click(await screen.findByTestId('restart-bitport-api'))
    fireEvent.click(screen.getByTestId('restart-confirm'))
    await waitFor(() => expect(restartHostUnit).toHaveBeenCalledWith('bitport-api'))
    expect(await screen.findByText('bitport-api is restarting')).toBeTruthy()
  })

  it('shows nothing to an account that is not a superadmin', async () => {
    fetchHostServices.mockResolvedValue({ ok: false, msg: 'superadmin only' })
    const { container } = render(<ServicesPanel />)
    await waitFor(() => expect(fetchHostServices).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
