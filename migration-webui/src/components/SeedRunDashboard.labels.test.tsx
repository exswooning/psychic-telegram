/**
 * Two tiles side by side read MESSAGES and CHAT MESSAGES. The first looks
 * like a total that ought to include the second; it is Gmail alone. On a
 * real run that was 277,440 against 0, and the only way to know which was
 * which was to know how the seeder prints its log.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import SeedRunDashboard from './SeedRunDashboard'

const LOG = [
  '  [a@x.test] starting (Eng, P)',
  '  [a@x.test] done in 10.0s: 2 files, 1 folders, 3 comments, 1443 messages,'
  + ' 4 drafts, 482 events, 2 secondary calendars, 0 chat messages in 0 spaces,'
  + ' 25 contacts, 20 tasks',
].join('\n')

const view = () => render(<SeedRunDashboard lines={LOG.split('\n')} />)

describe('the email counter says email', () => {
  it('is labelled emails, not messages', () => {
    view()
    expect(screen.getByTestId('stat-messages')).toHaveTextContent(/emails/i)
  })

  it('does not call itself just "messages"', () => {
    /* Beside CHAT MESSAGES, that reads as a total including it. */
    view()
    const tile = screen.getByTestId('stat-messages')
    expect(tile.textContent).not.toMatch(/^\s*messages/i)
  })

  it('still shows the number it always did', () => {
    view()
    expect(screen.getByTestId('stat-messages')).toHaveTextContent('1,443')
  })

  it('chat messages stay distinctly chat', () => {
    view()
    expect(screen.getByTestId('stat-chat messages'))
      .toHaveTextContent(/chat messages/i)
  })

  it('drafts say they are email drafts', () => {
    view()
    expect(screen.getByTestId('stat-drafts')).toHaveTextContent(/email drafts/i)
  })
})

describe('the testid still names the datum', () => {
  it('so a renamed label does not move where tests look', () => {
    view()
    expect(screen.getByTestId('stat-messages')).toBeInTheDocument()
    expect(screen.getByTestId('stat-files')).toBeInTheDocument()
  })
})
