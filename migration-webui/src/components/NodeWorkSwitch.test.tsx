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
  it('names no services at all', async () => {
    /* main.py's --services already defaults to "all" -- everything the
       tenant has configured. The box that used to be here defaulted to
       gmail, which is wrong wherever Google's own Data Migration Service is
       handling the mail, and it overrode a tenant setting from a page that
       is not where that setting lives. */
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-start'))
    await waitFor(() => expect(setDirective).toHaveBeenCalledWith(68, true))
  })

  it('sends run=false to stop', async () => {
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-stop'))
    await waitFor(() => expect(setDirective).toHaveBeenCalledWith(68, false))
  })

  it('says nodes pick it up on a poll, not instantly', async () => {
    /* A button implying instant remote control would be lying about what
       just happened -- nothing was pushed anywhere. */
    render(<NodeWorkSwitch accountId={68} />)
    fireEvent.click(screen.getByTestId('work-start'))
    const note = await screen.findByTestId('work-started')
    expect(note).toHaveTextContent('poll interval')
    // Asserted on the meaning, not the filename: the agent installs itself
    // as a service now, so naming the script would date the message.
    expect(note).toHaveTextContent('agent is not running')
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

describe('what it says about scope', () => {
  it('points at where services are actually configured', () => {
    render(<NodeWorkSwitch accountId={68} />)
    expect(screen.getByTestId('work-switch')).toHaveTextContent('Other services')
  })

  it('has no services input any more', () => {
    render(<NodeWorkSwitch accountId={68} />)
    expect(screen.queryByTestId('work-services')).toBeNull()
  })

  it('explains that claiming is the chunking', () => {
    /* "take the entire work and do a chunk" -- it already does: each node
       claims users one at a time from the coordinator. */
    render(<NodeWorkSwitch accountId={68} />)
    expect(screen.getByTestId('work-switch'))
      .toHaveTextContent('one at a time')
  })
})
