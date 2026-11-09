/** The list behind "N links answer 404": what the source answers per file, filterable, nothing when no check has run. */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import DeadLinksPanel from './DeadLinksPanel'

const api = vi.hoisted(() => ({ fetchDeadLinks: vi.fn(), deadLinksCsvUrl: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({ fetchDeadLinks: api.fetchDeadLinks, deadLinksCsvUrl: api.deadLinksCsvUrl }))

const view = {
  accountId: 3,
  totals: {
    NOT_FOUND: { label: 'Source file answers 404', links: 1244, emails: 3000 },
    NO_ACCESS: { label: 'No access to the source file', links: 0, emails: 0 },
    NOT_MIGRATED: { label: 'Source file exists, no copy on the target', links: 12, emails: 30 },
    OTHER: { label: 'Could not be checked', links: 0, emails: 0 },
  },
  matched: 1, offset: 0, limit: 100,
  links: [{ user: 'ward14@s.x.com', fileId: '1AbC', status: 'NOT_FOUND', detail: '', emails: 40, exampleMessage: 'm1',
            checkedAt: '2026-11-09T13:00:00Z', sourceUrl: 'https://drive.google.com/open?id=1AbC' }],
}

beforeEach(() => {
  api.fetchDeadLinks.mockReset()
  api.fetchDeadLinks.mockResolvedValue(view)
  api.deadLinksCsvUrl.mockReturnValue('/api/v2/mail-links?account_id=3&format=csv')
})

describe('DeadLinksPanel', () => {
  it('shows each kind with its files and emails and lists the links', async () => {
    render(<DeadLinksPanel accountId={3} />)
    expect(await screen.findByTestId('dead-kind-NOT_FOUND')).toHaveTextContent('1,244 files · 3,000 emails')
    expect(screen.queryByTestId('dead-kind-NO_ACCESS')).toBeNull()               // none of that kind: no chip
    expect(screen.getByTestId('dead-links-table')).toHaveTextContent('ward14')
    expect(screen.getByText('1AbC').closest('a')).toHaveAttribute('href', 'https://drive.google.com/open?id=1AbC')
  })

  it('a kind filters, and pressing it again clears the filter', async () => {
    render(<DeadLinksPanel accountId={3} />)
    fireEvent.click(await screen.findByTestId('dead-kind-NOT_MIGRATED'))
    await waitFor(() => expect(api.fetchDeadLinks).toHaveBeenLastCalledWith(3, 'NOT_MIGRATED', ''))
    fireEvent.click(screen.getByTestId('dead-kind-NOT_MIGRATED'))
    await waitFor(() => expect(api.fetchDeadLinks).toHaveBeenLastCalledWith(3, null, ''))
  })

  it('shows nothing until a check has run', async () => {
    api.fetchDeadLinks.mockResolvedValue({ ...view, totals: {}, links: [], matched: 0 })
    const { container } = render(<DeadLinksPanel accountId={3} />)
    await waitFor(() => expect(api.fetchDeadLinks).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})
