import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import WorkspacePageBoundary from './WorkspacePageBoundary'
describe('page failures leave a recoverable workspace', () => {
  it('renders ordinary pages unchanged', () => {
    render(<WorkspacePageBoundary><h1>Jobs</h1></WorkspacePageBoundary>)
    expect(screen.getByRole('heading', { name: 'Jobs' })).toBeInTheDocument()
  })
  it('shows a retry instead of a blank page and recovers', () => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    let fail = true
    function Page() { if (fail) throw new Error('fixture failure'); return <h1>Recovered</h1> }
    render(<WorkspacePageBoundary><Page /></WorkspacePageBoundary>)
    expect(screen.getByRole('alert')).toHaveTextContent('Couldn’t open this page')
    fail = false
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(screen.getByRole('heading', { name: 'Recovered' })).toBeInTheDocument()
    log.mockRestore()
  })
})
