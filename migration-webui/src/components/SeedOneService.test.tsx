/**
 * A twelve-hour seed produced 126 identical chat 404s -- one per user --
 * because the Chat app was not configured. Everything else in that corpus
 * is good, and the only way to recover chat was to seed all of it again.
 *
 * This is the way out, so what it must not do is become a way to quietly
 * damage the corpus it is meant to repair.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import SeedOneService, { SERVICES } from './SeedOneService'
import * as client from '@/api/client'

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof client>('@/api/client')
  return { ...actual, runSeed: vi.fn() }
})

beforeEach(() => {
  vi.mocked(client.runSeed).mockReset()
  vi.mocked(client.runSeed).mockResolvedValue({ ok: true })
})

const open = async () => {
  render(<SeedOneService domain="src.example" />)
  fireEvent.click(await screen.findByTestId('seed-one-src.example'))
  return screen.findByTestId('seed-one-confirm')
}

const confirm = (d = 'src.example') =>
  fireEvent.change(screen.getByTestId('seed-one-confirm'), { target: { value: d } })

describe('it is gated like everything else that writes to a tenant', () => {
  it('will not run until the domain is typed back', async () => {
    await open()
    expect(screen.getByTestId('seed-one-go')).toBeDisabled()
  })

  it('and not on the wrong domain', async () => {
    await open()
    confirm('tgt.example')
    expect(screen.getByTestId('seed-one-go')).toBeDisabled()
  })

  it('runs once it matches', async () => {
    await open()
    confirm()
    fireEvent.click(screen.getByTestId('seed-one-go'))
    await waitFor(() => expect(client.runSeed).toHaveBeenCalled())
  })
})

describe('it seeds only what was asked for', () => {
  it('sends the chosen service and nothing else', async () => {
    await open()
    confirm()
    fireEvent.click(screen.getByTestId('seed-one-go'))
    await waitFor(() => {
      const opts = vi.mocked(client.runSeed).mock.calls[0][4]
      expect(opts).toMatchObject({ only: 'chat' })
    })
  })

  it('does not create users -- they already exist', async () => {
    /* This tops up a corpus. Creating accounts would burn licences and
       leave a tenant that no longer matches its own manifest. */
    await open()
    confirm()
    fireEvent.click(screen.getByTestId('seed-one-go'))
    await waitFor(() => {
      const [, , createUsers, reset] = vi.mocked(client.runSeed).mock.calls[0]
      expect(createUsers).toBe(false)
      expect(reset).toBe(false)
    })
  })

  it('never resets, which would delete the corpus it is repairing', async () => {
    await open()
    confirm()
    fireEvent.click(screen.getByTestId('seed-one-go'))
    await waitFor(() =>
      expect(vi.mocked(client.runSeed).mock.calls[0][3]).toBe(false))
  })

  it('defaults to chat, which is the case that produced it', async () => {
    await open()
    expect(screen.getByTestId('seed-one-service')).toHaveValue('chat')
  })

  it('offers every service the seeder actually writes', () => {
    expect([...SERVICES]).toEqual(
      ['drive', 'gmail', 'calendar', 'chat', 'contacts', 'tasks'])
  })
})

describe('it warns about the one service that is not a top-up', () => {
  it('says so for drive', async () => {
    /* Drive BUILDS a corpus rather than adding to one, so running it on a
       tenant that already has one produces a second -- and the counts stop
       matching the manifest the run is judged against. */
    await open()
    fireEvent.change(screen.getByTestId('seed-one-service'),
                     { target: { value: 'drive' } })
    expect(await screen.findByText(/adds a second one/i)).toBeInTheDocument()
  })

  it('and not for chat', async () => {
    await open()
    expect(screen.queryByText(/adds a second one/i)).toBeNull()
  })
})

describe('what it says afterwards', () => {
  it('reports a queued run as queued, not started', async () => {
    vi.mocked(client.runSeed).mockResolvedValue({
      ok: true, queued: true, msg: 'the box is busy — queued at position 2' })
    await open()
    confirm()
    fireEvent.click(screen.getByTestId('seed-one-go'))
    expect(await screen.findByText(/queued at position 2/)).toBeInTheDocument()
  })

  it('surfaces a refusal instead of silently doing nothing', async () => {
    vi.mocked(client.runSeed).mockResolvedValue({
      ok: false, error: 'seeding is not enabled on this account' })
    await open()
    confirm()
    fireEvent.click(screen.getByTestId('seed-one-go'))
    expect(await screen.findByText(/not enabled on this account/))
      .toBeInTheDocument()
  })
})
