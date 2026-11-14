/**
 * The Sharing section: every grant the migration made, split inside / outside / public, from the
 * ledger. A kind is a filter; an older grant with no recorded role says so instead of guessing.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import SharingPanel from './SharingPanel'

const api = vi.hoisted(() => ({ fetchSharing: vi.fn(), sharingCsvUrl: vi.fn() }))
vi.mock('@/api/controlPlane', () => ({ fetchSharing: api.fetchSharing, sharingCsvUrl: api.sharingCsvUrl }))

const view = {
  accountId: 3, domains: ['s.x.com', 't.x.com'],
  totals: {
    external: { label: 'External people', grants: 12406, files: 5369 },
    public: { label: 'Anyone with the link', grants: 2742, files: 2742 },
    internal: { label: 'Inside the organisation', grants: 2111, files: 307 },
    domain: { label: 'A whole outside domain', grants: 1, files: 1 },
  },
  roles: { reader: 10, 'not recorded': 17000 }, notGranted: { OWED_GRANTEE_NO_ACCOUNT: 2 },
  externalPeople: [{ name: 'friend@gmail.com', files: 40 }], externalDomains: [{ name: 'gmail.com', files: 5000 }],
  matched: 1, offset: 0, limit: 50,
  grants: [{ kind: 'external', grantee: 'friend@gmail.com', owner: 'ward1@s.x.com', file: 'Budget.xlsx', role: null,
             sourceId: 'f1', targetId: 't1', sourceUrl: 'https://drive.google.com/open?id=f1',
             targetUrl: 'https://drive.google.com/open?id=t1' }],
}

beforeEach(() => {
  api.fetchSharing.mockReset()
  api.fetchSharing.mockResolvedValue(view)
  api.sharingCsvUrl.mockReturnValue('/api/v2/sharing?account_id=3&format=csv')
})

describe('SharingPanel', () => {
  it('shows each kind with its grants and files, and an older grant as role not recorded', async () => {
    render(<SharingPanel accountId={3} />)
    expect(await screen.findByTestId('sharing-kind-public')).toHaveTextContent('Anyone with the link: 2,742 grants · 2,742 files')
    expect(screen.getByTestId('sharing-kind-external')).toHaveTextContent('12,406 grants · 5,369 files')
    expect(screen.getByTestId('sharing-table')).toHaveTextContent('not recorded')
    expect(screen.getByText('Budget.xlsx').closest('a')).toHaveAttribute('href', 'https://drive.google.com/open?id=t1')
    expect(screen.getByTestId('sharing-roles')).toHaveTextContent('OWED_GRANTEE_NO_ACCOUNT 2')
  })

  it('a kind filters the list, and pressing it again clears the filter', async () => {
    render(<SharingPanel accountId={3} />)
    fireEvent.click(await screen.findByTestId('sharing-kind-public'))
    await waitFor(() => expect(api.fetchSharing).toHaveBeenLastCalledWith(3, 'public', '', 0, 50))
    fireEvent.click(screen.getByTestId('sharing-kind-public'))
    await waitFor(() => expect(api.fetchSharing).toHaveBeenLastCalledWith(3, null, '', 0, 50))
  })

  it('a migration with no grants shows nothing', async () => {
    api.fetchSharing.mockResolvedValue({ ...view, totals: Object.fromEntries(
      Object.entries(view.totals).map(([k, v]) => [k, { ...v, grants: 0, files: 0 }])) })
    const { container } = render(<SharingPanel accountId={3} />)
    await waitFor(() => expect(api.fetchSharing).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})
