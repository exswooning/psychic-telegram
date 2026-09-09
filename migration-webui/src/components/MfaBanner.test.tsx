/**
 * The banner exists because of a specific, expensive silence.
 *
 * Every browser sign-in this tool drives runs headless on the server. When
 * Google answers the password with "Check your phone -- tap 47", that page
 * is drawn to a framebuffer nobody watches: the setup stops making progress
 * for up to ten minutes and then reports "likely 2FA/captcha" -- a guess
 * about something it was looking straight at. Measured live on this box:
 * a Chat app configuration sat on "entered the password" until it was
 * killed, saying nothing.
 */
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import MfaBanner from './MfaBanner'

describe('when nothing is asking', () => {
  it('renders nothing at all', () => {
    const { container } = render(<MfaBanner challenge={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('and an empty string is nothing', () => {
    /* The watcher reports "" the moment the prompt clears. A falsy check
       that only tested null would leave the banner up for the rest of the
       run. */
    const { container } = render(<MfaBanner challenge="" />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('when a phone is asking', () => {
  const CHALLENGE =
    'Check your phone / Google sent a notification to your Pixel 7. Tap Yes, ' +
    'then tap 47 on your phone. / 47'

  it('shows up', () => {
    render(<MfaBanner challenge={CHALLENGE} />)
    expect(screen.getByTestId('mfa-banner')).toBeInTheDocument()
  })

  it('leads with what the prompt itself leads with', () => {
    render(<MfaBanner challenge={CHALLENGE} />)
    expect(screen.getByText('Check your phone')).toBeInTheDocument()
  })

  it('shows the number to tap, which is the whole point', () => {
    render(<MfaBanner challenge={CHALLENGE} />)
    expect(screen.getByTestId('mfa-banner')).toHaveTextContent('47')
  })

  it('names the device, so the right phone gets picked up', () => {
    render(<MfaBanner challenge={CHALLENGE} />)
    expect(screen.getByTestId('mfa-banner')).toHaveTextContent('Pixel 7')
  })

  it('says the server cannot answer it for you', () => {
    /* Without this the natural reading is "it is working on it". */
    render(<MfaBanner challenge={CHALLENGE} />)
    expect(screen.getByTestId('mfa-banner'))
      .toHaveTextContent(/cannot do it for you/i)
  })

  it('announces itself to a screen reader', () => {
    render(<MfaBanner challenge={CHALLENGE} />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('handles a prompt with no separators', () => {
    render(<MfaBanner challenge="Touch your security key" />)
    expect(screen.getByText('Touch your security key')).toBeInTheDocument()
  })
})
