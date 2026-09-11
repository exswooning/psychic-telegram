/**
 * Starting a node's work meant SSH-ing in and running main.py. The button
 * that replaces that must not become a way to reach into a machine: it
 * writes desired state, and each node pulls it.
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import NodeWorkSwitch from './NodeWorkSwitch'

const setDirective = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  setNodeDirective: (...a: unknown[]) => setDirective(...a),
}))

beforeEach(() => { setDirective.mockReset(); setDirective.mockResolvedValue({}) })

describe('starting and stopping', () => {
  it('sends run=true with the chosen services', async () => {
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-start'))
    await waitFor(() => expect(setDirective).toHaveBeenCalledWith(68, true, 'gmail'))
  })

  it('sends run=false to stop', async () => {
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-stop'))
    await waitFor(() => expect(setDirective).toHaveBeenCalledWith(68, false, ''))
  })

  it('says nodes pick it up on a poll, not instantly', async () => {
    /* A button implying instant remote control would be lying about what
       just happened -- nothing was pushed anywhere. */
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-start'))
    const note = await screen.findByTestId('work-started')
    expect(note).toHaveTextContent('poll interval')
    expect(note).toHaveTextContent('node_agent.py')
  })

  it('says stopping waits for the current user to finish', async () => {
    /* Cooperative on purpose: interrupting mid-user strands a mailbox and,
       on Drive, leaves items the local ledger never recorded. */
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-stop'))
    expect(await screen.findByTestId('work-stopped'))
      .toHaveTextContent('finishing the user')
  })

  it('does nothing at all without a tenant', () => {
    render(<NodeWorkSwitch accountId={undefined} />)
    expect(screen.getByTestId('work-start')).toBeDisabled()
    fireEvent.click(screen.getByTestId('work-start'))
    expect(setDirective).not.toHaveBeenCalled()
  })

  it('surfaces a refusal rather than looking like it worked', async () => {
    setDirective.mockRejectedValue(new Error('superadmin only'))
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-start'))
    expect(await screen.findByTestId('work-error')).toHaveTextContent('superadmin only')
    expect(screen.queryByTestId('work-started')).toBeNull()
  })
})
