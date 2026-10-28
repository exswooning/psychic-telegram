import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import WorkspaceSearch from './WorkspaceSearch'
function Location() { return <output data-testid="location">{useLocation().pathname}</output> }
describe('workspace tool search', () => {
  it('opens with the keyboard, filters capabilities and navigates', async () => {
    render(<MemoryRouter><WorkspaceSearch operator seed /><Location /></MemoryRouter>)
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    const input = await screen.findByRole('textbox', { name: 'Search all tools' })
    fireEvent.change(input, { target: { value: 'hybrid email' } })
    expect(screen.getByText('Jobs')).toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByTestId('location')).toHaveTextContent('/jobs')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
  it('keeps operator tools out of regular-account results', async () => {
    render(<MemoryRouter><WorkspaceSearch operator={false} seed={false} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Find a tool' }))
    const input = await screen.findByRole('textbox', { name: 'Search all tools' })
    fireEvent.change(input, { target: { value: 'teardown' } })
    expect(screen.getByText(/No matching tools/)).toBeInTheDocument()
    expect(screen.queryByText('GCP Teardown')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close tool search' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})
