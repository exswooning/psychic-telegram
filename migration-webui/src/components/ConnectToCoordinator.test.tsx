/**
 * A machine showing this page does not need a terminal to become a node --
 * it can redeem the join code itself.
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import ConnectToCoordinator from './ConnectToCoordinator'

const connect = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  connectToCoordinator: (...a: unknown[]) => connect(...a),
}))

beforeEach(() => { connect.mockReset() })

const openIt = () => {
  render(<ConnectToCoordinator />)
  fireEvent.click(screen.getByTestId('toggle-connect'))
}

describe('joining from the browser', () => {
  it('accepts the whole pasted command line', async () => {
    /* It is on the clipboard already; picking the URL out of it by hand is
       exactly the step that gets done wrong once and blamed on the tool. */
    connect.mockResolvedValue({ ok: true, coordinator: 'https://h.example',
                                nodeId: 'laptop', accountId: 68 })
    openIt()
    const cmd = 'irm https://h.example/api/v2/j/B95B-RZ5Z | iex'
    fireEvent.change(screen.getByTestId('connect-input'), { target: { value: cmd } })
    fireEvent.click(screen.getByTestId('connect-submit'))
    await waitFor(() => expect(connect).toHaveBeenCalledWith(
      expect.objectContaining({ command: cmd })))
  })

  it('confirms what it joined and that it is already running', async () => {
    /* Joining and running used to be two steps, and only the first
       happened -- a laptop sat "offline, 7h ago" on the coordinator with
       nothing anywhere explaining why. */
    connect.mockResolvedValue({ ok: true, coordinator: 'https://h.example',
                                nodeId: 'laptop', accountId: 68,
                                agentStarted: true, agentDetail: 'started' })
    openIt()
    fireEvent.change(screen.getByTestId('connect-input'), { target: { value: 'X' } })
    fireEvent.click(screen.getByTestId('connect-submit'))
    const ok = await screen.findByTestId('connect-ok')
    expect(ok).toHaveTextContent('h.example')
    expect(ok).toHaveTextContent('laptop')
    expect(ok).toHaveTextContent('agent is running')
  })

  it('says it will survive a reboot only when it actually will', async () => {
    connect.mockResolvedValue({ ok: true, coordinator: 'https://h.example',
                                nodeId: 'laptop', accountId: 68,
                                agentStarted: true, agentDetail: 'started' })
    openIt()
    fireEvent.change(screen.getByTestId('connect-input'), { target: { value: 'X' } })
    fireEvent.click(screen.getByTestId('connect-submit'))
    expect(await screen.findByTestId('connect-ok')).not.toHaveTextContent('reboot')
  })

  it('warns, and falls back to the command, when the agent will not start', async () => {
    /* A node that joined but has no agent is invisible and inert: offline
       on the coordinator, and Start does nothing to it. Saying so beats a
       green tick. */
    connect.mockResolvedValue({ ok: true, coordinator: 'https://h.example',
                                nodeId: 'laptop', accountId: 68,
                                agentStarted: false,
                                agentDetail: 'node_agent.py is not in this install' })
    openIt()
    fireEvent.change(screen.getByTestId('connect-input'), { target: { value: 'X' } })
    fireEvent.click(screen.getByTestId('connect-submit'))
    const warn = await screen.findByTestId('connect-no-agent')
    expect(warn).toHaveTextContent('not in this install')
    expect(warn).toHaveTextContent('node_agent.py')
    expect(screen.queryByTestId('connect-ok')).toBeNull()
  })

  it('shows why it failed rather than silently doing nothing', async () => {
    connect.mockRejectedValue(new Error('that join code has already been used'))
    openIt()
    fireEvent.change(screen.getByTestId('connect-input'), { target: { value: 'X' } })
    fireEvent.click(screen.getByTestId('connect-submit'))
    expect(await screen.findByTestId('connect-error'))
      .toHaveTextContent('already been used')
  })

  it('will not submit an empty box', () => {
    openIt()
    expect(screen.getByTestId('connect-submit')).toBeDisabled()
  })

  it('stays closed until asked', () => {
    render(<ConnectToCoordinator />)
    expect(screen.queryByTestId('connect-input')).toBeNull()
  })
})
