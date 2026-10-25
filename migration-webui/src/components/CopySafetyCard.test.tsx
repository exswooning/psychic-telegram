import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import CopySafetyCard from './CopySafetyCard'

const api = vi.hoisted(() => ({ fetchCopySafety: vi.fn(), startCopySafety: vi.fn() }))
vi.mock('@/api/controlPlane', () => api)

const report = {
  user: 'u@s.test', started: '2026-10-25T00:00:00Z', finished: '', overall: 'UNSAFE', file: 'x.json',
  verdicts: { doc: 'SAFE', form: 'UNSAFE', script: 'no sample' }, unattributed: [], auditReadable: true,
  cleanup: { ok: true },
  rows: [
    { type: 'doc', name: 'Plan', copied: true, verdict: 'SAFE', evidence: [] },
    { type: 'form', name: 'Survey', copied: true, verdict: 'UNSAFE',
      evidence: [{ kind: 'added', name: 'Copy of Survey', id: 'x1', modifiedTime: 't1' }] },
  ],
}

describe('CopySafetyCard', () => {
  it('shows the latest report per type with its evidence', async () => {
    api.fetchCopySafety.mockResolvedValue(report)
    render(<CopySafetyCard accountId={3} users={['u@s.test']} />)
    expect(await screen.findByTestId('cs-overall')).toHaveTextContent('UNSAFE')
    expect(screen.getByText(/added Copy of Survey x1/)).toBeInTheDocument()
    expect(screen.getByText('no sample')).toBeInTheDocument()
  })

  it('starts the test for the viewed account after a Reason Code', async () => {
    api.fetchCopySafety.mockResolvedValue(null)
    api.startCopySafety.mockResolvedValue({ ok: true, actionId: 1, detail: 'started pid 9' })
    render(<CopySafetyCard accountId={3} users={['u@s.test']} />)
    expect(screen.getByTestId('cs-start')).toBeDisabled()
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByRole('option', { name: 'u@s.test' }))
    fireEvent.click(screen.getByTestId('cs-start'))
    fireEvent.change(screen.getByLabelText('Reason Code'), { target: { value: 'dharan safety' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.startCopySafety).toHaveBeenCalledWith('dharan safety', 'u@s.test', 2, 10, 3))
  })
})
