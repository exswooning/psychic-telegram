import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

/* Live: the page said "No deploys recorded yet in this checkout" after five deploys
   in a day -- every one went through sync_vps.sh, which it never saw -- and told an
   operator on the server's own domain to open an SSH tunnel. */

const fetchDeployStatus = vi.fn()
vi.mock('@/api/client', () => ({
  fetchConfig: vi.fn().mockResolvedValue({}),
  saveDeployConfig: vi.fn(), runDeploy: vi.fn(),
  fetchDeployHistory: vi.fn().mockResolvedValue([]),
  fetchDeployStatus: (...a: unknown[]) => fetchDeployStatus(...a),
}))
vi.mock('@/api/controlPlane', () => ({
  getCpBase: () => '', setCpBase: vi.fn(), checkConnection: vi.fn(),
}))
vi.mock('@/components/JobProgress', () => ({ default: () => null }))

import Deploy from './Deploy'

const deploy = (over = {}) => ({
  at: '2026-10-04T09:42:42Z', commit: '9d7ae1c', subject: "A pair's end of life is policy",
  restarted: true, files: ['api_server.py', 'lifecycle.py'], from: 'mac', ...over })

describe('the Deploy page tells the truth about this server', () => {
  it('names the running commit and every recorded deploy', async () => {
    fetchDeployStatus.mockResolvedValue({ commit: '9d7ae1c', deployedAt: '2026-10-04T09:42:42Z', busy: [],
      history: [deploy(), deploy({ commit: '0ff37fa', subject: 'frontend tweak', restarted: false, files: [] })] })
    render(<Deploy />)
    const card = await screen.findByTestId('this-server')
    expect(card).toHaveTextContent("9d7ae1cA pair's end of life is policy")
    expect(card).toHaveTextContent('restarted (2 files)')
    expect(card).toHaveTextContent('frontend only')
    expect(card).toHaveTextContent('a deploy now interrupts nothing')
  })

  it('says which running jobs a restarting deploy would stop', async () => {
    fetchDeployStatus.mockResolvedValue({ commit: 'abc', deployedAt: null, busy: ['migrate (account 3)'], history: [] })
    render(<Deploy />)
    expect(await screen.findByTestId('deploy-busy')).toHaveTextContent('would stop: migrate (account 3)')
    expect(screen.getByText(/next sync_vps.sh run records the first/)).toBeTruthy()
  })
})
