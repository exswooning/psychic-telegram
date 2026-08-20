import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ReprovisionPanel from './ReprovisionPanel'

vi.mock('@/api/controlPlane', () => ({
  fetchScopeOptions: vi.fn(async () => ({
    side: 'source',
    required: [
      'https://www.googleapis.com/auth/drive',
      'https://www.googleapis.com/auth/gmail.modify',
    ],
    optional: ['https://www.googleapis.com/auth/apps.licensing'],
    default: [
      'https://www.googleapis.com/auth/drive',
      'https://www.googleapis.com/auth/gmail.modify',
      'https://www.googleapis.com/auth/apps.licensing',
    ],
  })),
}))

/**
 * Re-provisioning abandons a tenant's service account and client ID, so the
 * delegation in place stops applying until the run re-grants it. These pin
 * the two properties that keep that from happening by accident, and the one
 * that keeps a scope chooser from quietly breaking a migration.
 */
const open = async () => {
  fireEvent.click(screen.getByTestId('reprovision-toggle'))
  await waitFor(() => expect(screen.getByTestId('required-scopes')).toBeTruthy())
}

describe('ReprovisionPanel', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('is collapsed until asked for', () => {
    /* A destructive action must not sit open next to the ordinary ones. */
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={() => {}} />)
    expect(screen.queryByTestId('required-scopes')).toBeNull()
  })

  it('will not fire until the domain is typed back', async () => {
    const onReprovision = vi.fn()
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={onReprovision} />)
    await open()
    expect(screen.getByTestId('reprovision-go')).toBeDisabled()
    fireEvent.click(screen.getByTestId('reprovision-go'))
    expect(onReprovision).not.toHaveBeenCalled()
  })

  it('fires once the typed domain matches', async () => {
    const onReprovision = vi.fn()
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={onReprovision} />)
    await open()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'c.example.com' } })
    fireEvent.click(screen.getByTestId('reprovision-go'))
    expect(onReprovision).toHaveBeenCalledTimes(1)
  })

  it('does not accept a near-miss domain', async () => {
    /* Getting the wrong tenant here costs a working setup. */
    const onReprovision = vi.fn()
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={onReprovision} />)
    await open()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'a.example.com' } })
    expect(screen.getByTestId('reprovision-go')).toBeDisabled()
    expect(onReprovision).not.toHaveBeenCalled()
  })

  it('shows required scopes as fixed, with no way to deselect them', async () => {
    /* The important one. A delegated token request is all-or-nothing: ask
       for a scope the console has not authorised and the WHOLE exchange
       fails. So an unchecked "required" box would not produce a narrower
       migration, it would produce a tenant that cannot migrate -- diagnosed
       much later as a delegation gap. */
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={() => {}} />)
    await open()
    const required = screen.getByTestId('required-scopes')
    expect(required).toHaveTextContent('drive')
    expect(required).toHaveTextContent('gmail.modify')
    expect(screen.queryByTestId('scope-drive')).toBeNull()
    expect(screen.queryByTestId('scope-gmail.modify')).toBeNull()
    expect(screen.getByTestId('required-explainer'))
      .toHaveTextContent('always granted')
  })

  it('lets optional scopes be turned off, and reports the reduced set', async () => {
    const onReprovision = vi.fn()
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={onReprovision} />)
    await open()
    fireEvent.click(screen.getByTestId('scope-apps.licensing'))
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'c.example.com' } })
    fireEvent.click(screen.getByTestId('reprovision-go'))
    const sent: string[] = onReprovision.mock.calls[0][0]
    expect(sent).not.toContain('https://www.googleapis.com/auth/apps.licensing')
    expect(sent).toContain('https://www.googleapis.com/auth/drive')
  })

  it('starts with everything selected', async () => {
    /* The default has to be the working configuration, not an empty set
       someone has to reconstruct. */
    const onReprovision = vi.fn()
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={onReprovision} />)
    await open()
    fireEvent.change(screen.getByTestId('confirm-domain'),
                     { target: { value: 'c.example.com' } })
    fireEvent.click(screen.getByTestId('reprovision-go'))
    expect(onReprovision.mock.calls[0][0]).toHaveLength(3)
  })

  it('says what re-provisioning actually costs', async () => {
    render(<ReprovisionPanel side="source" domain="c.example.com"
                             onReprovision={() => {}} />)
    await open()
    const panel = screen.getByTestId('reprovision-panel')
    expect(panel).toHaveTextContent('new')
    expect(panel).toHaveTextContent('client ID')
    expect(panel).toHaveTextContent(/stops applying/)
  })
})
