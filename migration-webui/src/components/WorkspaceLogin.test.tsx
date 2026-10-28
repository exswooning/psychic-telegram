import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import WorkspaceLogin from './WorkspaceLogin'
const noop = () => {}
describe('workspace sign-in', () => {
  it('keeps submit disabled until credentials are entered', () => {
    render(<MemoryRouter><WorkspaceLogin email="" password="" busy={false} error="" onEmail={noop} onPassword={noop} onCloseError={noop} onSubmit={noop} /></MemoryRouter>)
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled()
    expect(screen.getByLabelText(/Password/)).toHaveAttribute('autocomplete', 'current-password')
  })
  it('submits through the existing handler and displays the server error', () => {
    const submit = vi.fn(e => e.preventDefault())
    render(<MemoryRouter><WorkspaceLogin email="test@example.test" password="fixture" busy={false} error="Sign-in failed" onEmail={noop} onPassword={noop} onCloseError={noop} onSubmit={submit} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(submit).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert')).toHaveTextContent('Sign-in failed')
  })
})
