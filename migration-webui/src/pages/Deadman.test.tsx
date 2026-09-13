/**
 * The most destructive automation on the machine, so every number is shown
 * with the signal it came from: a countdown saying "18h left" without
 * saying what reset it is asking to be trusted about a permanent wipe.
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Deadman from './Deadman'

const status = vi.fn()
const wipe = vi.fn()
const touch = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchDeadman: () => status(),
  deadmanWipeNow: (...a: unknown[]) => wipe(...a),
  deadmanTouch: () => touch(),
}))

const st = (over = {}) => ({
  armed: true, state: 'armed', days: 7,
  signals: { 'sshd auth': 120, 'web UI login': 4000, 'deploy': null },
  newestSignal: 'sshd auth', secondsSinceSeen: 120,
  secondsRemaining: 7 * 86400 - 120,
  targets: ['/root/migration/keys', '/etc/bitport'],
  emailConfigured: true, ...over,
})

beforeEach(() => {
  status.mockReset(); wipe.mockReset(); touch.mockReset()
  touch.mockResolvedValue({ ok: true, newestSignal: 'manual touch' })
  status.mockResolvedValue(st()); wipe.mockResolvedValue({ ok: true, removed: [] })
})

describe('the countdown', () => {
  it('shows time remaining when armed', async () => {
    render(<Deadman />)
    expect(await screen.findByTestId('countdown')).toHaveTextContent(/\d+d \d+h/)
  })

  it('says what last reset it', async () => {
    /* A countdown that does not say what is keeping it alive cannot be
       checked by the person relying on it. */
    render(<Deadman />)
    await screen.findByTestId('countdown')
    // Scoped to the countdown panel: the signal also appears in the
    // signals table below, which is a different claim about the same fact.
    expect(screen.getByTestId('countdown').parentElement)
      .toHaveTextContent('sshd auth')
  })

  it('does not count down when it is not armed', async () => {
    status.mockResolvedValue(st({ armed: false, state: 'not armed',
                                  secondsRemaining: null }))
    render(<Deadman />)
    expect(await screen.findByTestId('not-counting'))
      .toHaveTextContent('Nothing will be destroyed')
    expect(screen.queryByTestId('countdown')).toBeNull()
  })

  it('lists every signal, including the ones never seen', async () => {
    /* "never" on a signal is information: it says which route to reviving
       this box is not currently working. */
    render(<Deadman />)
    await screen.findByTestId('countdown')
    expect(screen.getByTestId('signal-deploy')).toHaveTextContent('never')
    expect(screen.getByTestId('signal-sshd-auth')).toHaveTextContent('ago')
  })

  it('warns when it cannot reach you', async () => {
    status.mockResolvedValue(st({ emailConfigured: false }))
    render(<Deadman />)
    expect(await screen.findByTestId('no-email'))
      .toHaveTextContent('log only')
  })
})

describe('wipe now', () => {
  it('shows exactly what would go', async () => {
    render(<Deadman />)
    expect(await screen.findByTestId('wipe-targets'))
      .toHaveTextContent('/etc/bitport')
  })

  it('will not fire until the word is typed', async () => {
    render(<Deadman />)
    fireEvent.click(await screen.findByTestId('wipe-now'))
    expect(await screen.findByTestId('wipe-go')).toBeDisabled()
    fireEvent.change(screen.getByTestId('wipe-confirm'),
                     { target: { value: 'yes' } })
    expect(screen.getByTestId('wipe-go')).toBeDisabled()
    fireEvent.change(screen.getByTestId('wipe-confirm'),
                     { target: { value: 'WIPE' } })
    expect(screen.getByTestId('wipe-go')).not.toBeDisabled()
  })

  it('sends the reason with it', async () => {
    /* The audit row survives the thing it records; a wipe with no reason is
       an event nobody can explain afterwards. */
    render(<Deadman />)
    fireEvent.click(await screen.findByTestId('wipe-now'))
    fireEvent.change(screen.getByTestId('wipe-reason'),
                     { target: { value: 'laptop stolen' } })
    fireEvent.change(screen.getByTestId('wipe-confirm'),
                     { target: { value: 'WIPE' } })
    fireEvent.click(screen.getByTestId('wipe-go'))
    await waitFor(() => expect(wipe).toHaveBeenCalledWith('laptop stolen'))
  })

  it('reports a refusal rather than looking like it worked', async () => {
    wipe.mockRejectedValue(new Error('type WIPE to confirm'))
    render(<Deadman />)
    fireEvent.click(await screen.findByTestId('wipe-now'))
    fireEvent.change(screen.getByTestId('wipe-confirm'),
                     { target: { value: 'WIPE' } })
    fireEvent.click(screen.getByTestId('wipe-go'))
    expect(await screen.findByTestId('deadman-error'))
      .toHaveTextContent('type WIPE')
  })
})

describe('resetting the timer', () => {
  it('offers an explicit reset while it is counting down', async () => {
    /* Being signed in is NOT a signal: a session row is written at LOGIN,
       so somebody already signed in could watch this page reach zero while
       looking straight at it. That is the worst failure this feature could
       have. */
    render(<Deadman />)
    expect(await screen.findByTestId('deadman-touch')).toBeInTheDocument()
  })

  it('resets and re-reads, so the number visibly moves', async () => {
    render(<Deadman />)
    fireEvent.click(await screen.findByTestId('deadman-touch'))
    await waitFor(() => expect(touch).toHaveBeenCalled())
    await waitFor(() => expect(status.mock.calls.length).toBeGreaterThan(1))
  })

  it('does not reset merely because the page was opened', async () => {
    /* A forgotten open tab must not hold a dead man switch alive for
       months -- which is exactly what "any page view counts" would mean. */
    render(<Deadman />)
    await screen.findByTestId('countdown')
    expect(touch).not.toHaveBeenCalled()
  })

  it('surfaces a refusal rather than looking like it reset', async () => {
    touch.mockRejectedValue(new Error('superadmin only'))
    render(<Deadman />)
    fireEvent.click(await screen.findByTestId('deadman-touch'))
    expect(await screen.findByTestId('deadman-error'))
      .toHaveTextContent('superadmin only')
  })
})
