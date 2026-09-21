/**
 * The completion curve and totals bar are built from the SAME parsed data
 * the Stat tiles already show -- this pins that they only appear once
 * there is a real shape to draw, and that they read the run's own numbers
 * rather than a fabricated trend.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import SeedRunDashboard from './SeedRunDashboard'

const TWO_USERS = [
  '  [a@x.test] starting (Eng, P)',
  '  [b@x.test] starting (Eng, P)',
  '  [a@x.test] done in 10.0s: 2 files, 1 folders, 3 comments, 5 messages,'
  + ' 1 drafts, 2 events, 0 secondary calendars, 1 chat messages in 1 spaces,'
  + ' 3 contacts, 2 tasks',
  '  [b@x.test] done in 20.0s: 4 files, 2 folders, 6 comments, 9 messages,'
  + ' 2 drafts, 4 events, 0 secondary calendars, 2 chat messages in 1 spaces,'
  + ' 6 contacts, 4 tasks',
].join('\n')

const ONE_USER = [
  '  [a@x.test] starting (Eng, P)',
  '  [a@x.test] done in 10.0s: 2 files, 1 folders, 3 comments, 5 messages,'
  + ' 1 drafts, 2 events, 0 secondary calendars, 1 chat messages in 1 spaces,'
  + ' 3 contacts, 2 tasks',
].join('\n')

describe('the completion curve', () => {
  it('does not render for a single finished user -- one point has no trend', () => {
    render(<SeedRunDashboard lines={ONE_USER.split('\n')} />)
    expect(screen.queryByText(/finished over time/i)).toBeNull()
  })

  it('renders once at least two users have finished', () => {
    render(<SeedRunDashboard lines={TWO_USERS.split('\n')} />)
    expect(screen.getByText(/finished over time/i)).toBeInTheDocument()
  })
})

describe('the totals bar', () => {
  it('renders alongside the exact-count tiles, not instead of them', () => {
    render(<SeedRunDashboard lines={TWO_USERS.split('\n')} />)
    // The tiles still carry the real, exact numbers -- the chart adds the
    // shape, it does not replace the count anyone would actually cite.
    expect(screen.getByTestId('stat-messages')).toHaveTextContent('14')
    expect(document.querySelector('.recharts-responsive-container')).toBeInTheDocument()
  })
})
