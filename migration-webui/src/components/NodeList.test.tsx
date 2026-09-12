/**
 * The page handed out join codes and started work but never showed what had
 * joined -- so a node that installed cleanly and one that never checked in
 * looked identical from here.
 */
import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import NodeList from './NodeList'

const fleet = vi.fn()
const takesWork = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchFleet: () => fleet(),
  setNodeTakesWork: (...a: unknown[]) => takesWork(...a),
}))

const node = (over = {}) => ({
  node_id: 'DESKTOP-6T3O8A3', hostname: null, location: null,
  code_commit: null, last_seen: new Date().toISOString(),
  cpu_pct: null, ram_pct: null, disk_pct: null, active_job: null,
  job_pid: null, transfer_mode: null, users_done: 3, users_running: 0,
  users_failed: 0, error_rate: 0, healthy: true, secondsSinceHeartbeat: 4,
  takes_work: 1,
  ...over,
})

beforeEach(() => {
  fleet.mockReset(); takesWork.mockReset()
  fleet.mockResolvedValue([]); takesWork.mockResolvedValue({})
})

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

describe('choosing which machines work', () => {
  it('excludes one without touching the tenant run', async () => {
    /* The node ANDs this with the tenant's directive on its next poll, so
       sitting one laptop out does not stop the others. */
    fleet.mockResolvedValue([node()])
    render(<NodeList />)
    const sw = await screen.findByTestId('takes-DESKTOP-6T3O8A3')
    fireEvent.click(sw.querySelector('input')!)
    await waitFor(() =>
      expect(takesWork).toHaveBeenCalledWith('DESKTOP-6T3O8A3', false))
  })

  it('puts one back in', async () => {
    fleet.mockResolvedValue([node({ takes_work: 0 })])
    render(<NodeList />)
    const sw = await screen.findByTestId('takes-DESKTOP-6T3O8A3')
    expect(sw.querySelector('input')).not.toBeChecked()
    fireEvent.click(sw.querySelector('input')!)
    await waitFor(() =>
      expect(takesWork).toHaveBeenCalledWith('DESKTOP-6T3O8A3', true))
  })

  it('treats a machine with no flag as taking work', async () => {
    /* DEFAULT 1 in the schema: every node that joined before this column
       existed was already working, and upgrading must not silently idle a
       fleet. */
    fleet.mockResolvedValue([node({ takes_work: undefined })])
    render(<NodeList />)
    const sw = await screen.findByTestId('takes-DESKTOP-6T3O8A3')
    expect(sw.querySelector('input')).toBeChecked()
  })

  it('re-reads the truth if the change is refused', async () => {
    /* The switch moves optimistically so it feels immediate; a refusal must
       not leave it showing a state the server never accepted. */
    fleet.mockResolvedValue([node()])
    takesWork.mockRejectedValue(new Error('nope'))
    render(<NodeList />)
    const sw = await screen.findByTestId('takes-DESKTOP-6T3O8A3')
    const before = fleet.mock.calls.length
    fireEvent.click(sw.querySelector('input')!)
    await waitFor(() => expect(fleet.mock.calls.length).toBeGreaterThan(before))
  })
})
