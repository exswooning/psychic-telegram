/**
 * The page handed out join codes and started work but never showed what had
 * joined -- so a node that installed cleanly and one that never checked in
 * looked identical from here.
 */
import React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import NodeList from './NodeList'

const fleet = vi.fn()
vi.mock('@/api/controlPlane', () => ({ fetchFleet: () => fleet() }))

const node = (over = {}) => ({
  node_id: 'DESKTOP-6T3O8A3', hostname: null, location: null,
  code_commit: null, last_seen: new Date().toISOString(),
  cpu_pct: null, ram_pct: null, disk_pct: null, active_job: null,
  job_pid: null, transfer_mode: null, users_done: 3, users_running: 0,
  users_failed: 0, error_rate: 0, healthy: true, secondsSinceHeartbeat: 4,
  ...over,
})

beforeEach(() => { fleet.mockReset(); fleet.mockResolvedValue([]) })

describe('listing machines', () => {
  it('says none rather than showing an empty table', async () => {
    render(<NodeList />)
    expect(await screen.findByTestId('no-nodes')).toBeInTheDocument()
  })

  it('shows a live machine as online', async () => {
    fleet.mockResolvedValue([node()])
    render(<NodeList />)
    expect(await screen.findByTestId('state-DESKTOP-6T3O8A3'))
      .toHaveTextContent('online')
  })

  it('shows one that stopped reporting as offline', async () => {
    /* Derived from last_seen at read time, not stored: a node that dies
       cannot mark itself down, which is the whole failure mode. */
    fleet.mockResolvedValue([node({ healthy: false, secondsSinceHeartbeat: 4000 })])
    render(<NodeList />)
    expect(await screen.findByTestId('state-DESKTOP-6T3O8A3'))
      .toHaveTextContent('offline')
  })

  it('shows what it is doing, or idle', async () => {
    fleet.mockResolvedValue([node({ active_job: 'migrate all' })])
    render(<NodeList />)
    await waitFor(() =>
      expect(screen.getByTestId('node-DESKTOP-6T3O8A3')).toHaveTextContent('migrate all'))
  })

  it('survives the fleet read failing', async () => {
    fleet.mockRejectedValue(new Error('nope'))
    render(<NodeList />)
    expect(await screen.findByTestId('no-nodes')).toBeInTheDocument()
  })
})
