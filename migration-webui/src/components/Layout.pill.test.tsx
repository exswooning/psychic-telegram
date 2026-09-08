import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

/* The pill is the only thing on screen that says work is happening, and it
   appears on every page. A reader who notices it has to be able to act on
   it -- before this, finding the run meant already knowing which page it
   lived on. */

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>(
    'react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

const client = vi.hoisted(() => ({ fetchJob: vi.fn() }))
vi.mock('@/api/client', () => ({
  fetchJob: client.fetchJob,
  fetchConfig: vi.fn().mockResolvedValue({ host: {} }),
  stopJob: vi.fn(),
}))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: vi.fn().mockResolvedValue({ id: 1, is_superadmin: true }),
  logout: vi.fn(),
}))

import Layout from './Layout'

describe('running job pill', () => {
  it('goes to Jobs when clicked', async () => {
    client.fetchJob.mockResolvedValue({
      running: true, name: 'wipe tenant data', elapsed: 23.9,
      progressPct: null, etaSeconds: null, lines: [],
    })
    render(<MemoryRouter><Layout><div /></Layout></MemoryRouter>)
    const pill = await screen.findByTestId('running-job-pill')
    fireEvent.click(pill)
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/jobs'))
  })

  it('is reachable from the keyboard', async () => {
    client.fetchJob.mockResolvedValue({
      running: true, name: 'seed', elapsed: 5,
      progressPct: 12, etaSeconds: null, lines: [],
    })
    render(<MemoryRouter><Layout><div /></Layout></MemoryRouter>)
    const pill = await screen.findByTestId('running-job-pill')
    expect(pill).toHaveAttribute('tabindex', '0')
    fireEvent.keyDown(pill, { key: 'Enter' })
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/jobs'))
  })

  it('says what it is for, for a screen reader', async () => {
    client.fetchJob.mockResolvedValue({
      running: true, name: 'seed', elapsed: 5,
      progressPct: null, etaSeconds: null, lines: [],
    })
    render(<MemoryRouter><Layout><div /></Layout></MemoryRouter>)
    const pill = await screen.findByTestId('running-job-pill')
    expect(pill.getAttribute('aria-label')).toMatch(/open Jobs/)
  })
})
