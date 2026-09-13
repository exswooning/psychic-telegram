/**
 * The 2-Step code on the page, so nobody has to find a phone.
 *
 * The wizard drives a real browser through Google's sign-in and a 2-Step
 * prompt stops it dead. Until now the UI could only DISPLAY the challenge
 * and tell the operator to go and deal with it, which is the whole reason
 * an unattended setup was not unattended.
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import AuthenticatorCode from './AuthenticatorCode'

const code = vi.fn()
const store = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMfaCode: (...a: unknown[]) => code(...a),
  storeMfaSecret: (...a: unknown[]) => store(...a),
}))

const ok = (over = {}) => ({
  accounts: ['admin@src.test'], email: 'admin@src.test',
  code: '481920', secondsRemaining: 22, period: 30, ...over,
})

beforeEach(() => {
  code.mockReset(); store.mockReset()
  code.mockResolvedValue(ok())
  store.mockResolvedValue({ ok: true, email: 'a@b.test', code: '000000',
                            secondsRemaining: 30 })
})

describe('showing the code', () => {
  it('shows it grouped, the way an authenticator app does', async () => {
    render(<AuthenticatorCode email="admin@src.test" />)
    expect(await screen.findByTestId('mfa-code')).toHaveTextContent('481 920')
  })

  it('shows how long is left as loudly as the digits', async () => {
    /* A code with two seconds on it is rejected by the time it is typed,
       and a UI showing only the digits invites exactly that. */
    render(<AuthenticatorCode email="admin@src.test" />)
    expect(await screen.findByTestId('mfa-seconds')).toHaveTextContent('22')
  })

  it('says to wait when the window is nearly closed', async () => {
    code.mockResolvedValue(ok({ secondsRemaining: 3 }))
    render(<AuthenticatorCode email="admin@src.test" />)
    expect(await screen.findByTestId('mfa-expiring'))
      .toHaveTextContent('wait for the next one')
  })

  it('copies the digits alone, not the spaced form', async () => {
    const writeText = vi.fn()
    Object.assign(navigator, { clipboard: { writeText } })
    render(<AuthenticatorCode email="admin@src.test" />)
    fireEvent.click(await screen.findByTestId('mfa-copy'))
    expect(writeText).toHaveBeenCalledWith('481920')
  })

  it('asks the server for the account it was given', async () => {
    render(<AuthenticatorCode email="admin@src.test" />)
    await waitFor(() => expect(code).toHaveBeenCalledWith('admin@src.test'))
  })
})

describe('when no seed is stored', () => {
  it('offers to take one', async () => {
    code.mockResolvedValue(ok({ accounts: [], code: '',
                                error: 'no authenticator seed stored for x' }))
    render(<AuthenticatorCode email="x@y.test" />)
    expect(await screen.findByTestId('mfa-new-secret')).toBeInTheDocument()
  })

  it('states the trade-off rather than burying it', async () => {
    /* A seed beside the password is one factor, not two. Somebody enabling
       this should read that at the moment they enable it, not afterwards. */
    code.mockResolvedValue(ok({ accounts: [], code: '' }))
    render(<AuthenticatorCode />)
    expect(await screen.findByTestId('mfa-tradeoff'))
      .toHaveTextContent('one factor')
  })

  it('will not save an empty field', async () => {
    code.mockResolvedValue(ok({ accounts: [], code: '' }))
    render(<AuthenticatorCode />)
    expect(await screen.findByTestId('mfa-save')).toBeDisabled()
  })

  it('saves what was pasted and re-reads', async () => {
    code.mockResolvedValue(ok({ accounts: [], code: '' }))
    render(<AuthenticatorCode />)
    fireEvent.change(await screen.findByTestId('mfa-new-email'),
                     { target: { value: 'a@b.test' } })
    fireEvent.change(screen.getByTestId('mfa-new-secret'),
                     { target: { value: 'abcd efgh ijkl mnop' } })
    fireEvent.click(screen.getByTestId('mfa-save'))
    await waitFor(() => expect(store)
      .toHaveBeenCalledWith('a@b.test', 'abcd efgh ijkl mnop'))
  })

  it('reports a rejected secret instead of looking saved', async () => {
    code.mockResolvedValue(ok({ accounts: [], code: '' }))
    store.mockRejectedValue(new Error('that does not look like a valid authenticator secret'))
    render(<AuthenticatorCode />)
    fireEvent.change(await screen.findByTestId('mfa-new-email'),
                     { target: { value: 'a@b.test' } })
    fireEvent.change(screen.getByTestId('mfa-new-secret'),
                     { target: { value: 'not base32!' } })
    fireEvent.click(screen.getByTestId('mfa-save'))
    expect(await screen.findByTestId('mfa-error'))
      .toHaveTextContent('does not look like a valid')
  })
})

describe('adding a second account', () => {
  it('keeps the form available when asked to', async () => {
    /* The form showed only when NO seed existed, which is right inside the
       wizard -- there it rescues a prompt already on screen. On a page whose
       purpose is managing these, hiding it after the first made adding a
       second account impossible. */
    render(<AuthenticatorCode email="admin@src.test" allowAdd />)
    expect(await screen.findByTestId('mfa-new-secret')).toBeInTheDocument()
    expect(screen.getByTestId('mfa-code')).toHaveTextContent('481 920')
  })

  it('still hides it by default, where it is only a rescue', async () => {
    render(<AuthenticatorCode email="admin@src.test" />)
    await screen.findByTestId('mfa-code')
    expect(screen.queryByTestId('mfa-new-secret')).toBeNull()
  })
})

describe('the account chooser', () => {
  it('is absent until there is something to choose between', async () => {
    /* With no seeds stored it was a dropdown whose only entry was "select
       an account", sitting above the form for creating the first one. */
    code.mockResolvedValue(ok({ accounts: [], code: '' }))
    render(<AuthenticatorCode />)
    await screen.findByTestId('mfa-new-secret')
    expect(screen.queryByTestId('mfa-account')).toBeNull()
  })

  it('appears once an account exists', async () => {
    render(<AuthenticatorCode email="admin@src.test" />)
    expect(await screen.findByTestId('mfa-account')).toBeInTheDocument()
  })

  it('keeps its label clear of the option text', async () => {
    /* A native select does not take part in MUI's label-shrink logic, so
       "Account" rendered directly on top of the first option and both were
       unreadable. Only visible with a native select, which is why every
       value-based assertion passed while the page looked broken. */
    render(<AuthenticatorCode email="admin@src.test" />)
    await screen.findByTestId('mfa-account')
    const label = document.querySelector('label')
    expect(label?.className).toMatch(/MuiInputLabel-shrink/)
  })
})
