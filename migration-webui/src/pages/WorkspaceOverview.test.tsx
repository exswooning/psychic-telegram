import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMigrationStore } from '@/store'
vi.mock('@/api/client', () => ({ fetchConfig: vi.fn().mockResolvedValue({ config: { source_domain: 'source.example', target_domain: 'target.example' } }) }))
import WorkspaceOverview from './WorkspaceOverview'
describe('working workspace overview', () => {
  beforeEach(() => useMigrationStore.setState({ users: [], activities: [], verification: [], isLoading: false, error: null, lastUpdate: undefined }))
  it('shows absent evidence rather than claiming a match', async () => {
    render(<MemoryRouter><WorkspaceOverview /></MemoryRouter>)
    expect(await screen.findByText('source.example')).toBeInTheDocument()
    expect(screen.getByText('target.example')).toBeInTheDocument()
    expect(screen.getByText('No saved verification results yet.')).toBeInTheDocument()
    expect(screen.getAllByText('Not reported')).toHaveLength(7)
    expect(screen.getByText(/These are not verification results/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'All tools' })).toHaveAttribute('href', '/tools')
  })
  it('surfaces stale data without claiming 1:1 success', async () => {
    useMigrationStore.setState({ error: 'metrics request failed' })
    render(<MemoryRouter><WorkspaceOverview /></MemoryRouter>)
    expect(screen.getByRole('alert')).toHaveTextContent('Values may be out of date')
    expect(screen.queryByText('1:1 complete')).not.toBeInTheDocument()
    await screen.findByText('source.example')
  })
  it('opens service evidence, people and activity without starting a job', async () => {
    render(<MemoryRouter><WorkspaceOverview /></MemoryRouter>)
    await screen.findByText('source.example')
    fireEvent.click(screen.getByRole('button', { name: 'View Drive details' }))
    expect(await screen.findByText('Drive details')).toBeInTheDocument()
    expect(screen.getByText('No ledger data reported for this service.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close service details' }))
    fireEvent.click(screen.getByRole('tab', { name: 'People' }))
    expect(screen.getByText('No people have been loaded yet.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Activity' }))
    expect(screen.getByText('No recorded activity yet.')).toBeInTheDocument()
  })
  it('filters service findings using saved mismatch evidence', async () => {
    useMigrationStore.setState({ verification: [{ type: 'Drive files', status: 'mismatch', sourceCount: 3, targetCount: 2, confidence: 50 }] })
    render(<MemoryRouter><WorkspaceOverview /></MemoryRouter>)
    await screen.findByText('source.example')
    fireEvent.click(screen.getByRole('button', { name: 'Needs review 1' }))
    expect(screen.getByRole('button', { name: 'View Drive details' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'View Gmail details' })).not.toBeInTheDocument()
  })
})
