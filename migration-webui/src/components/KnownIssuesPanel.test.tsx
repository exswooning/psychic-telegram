import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

vi.mock('@/api/controlPlane', () => ({
  fetchKnownIssues: () => Promise.resolve({
    accountId: 5,
    kinds: [
      { id: 'gmail.labels_added', service: 'gmail', title: 'Gmail added a label the source message does not have',
        cause: 'Filed as received mail.', fix: 'Remove the extra labels on the target copy.', auto: true,
        open: 2, fixed: 4329, lastFixed: '2026-10-23T09:00:00Z' },
      { id: 'drive.group_grant', service: 'drive', title: "A group's share did not come across",
        cause: 'The group does not exist on the target.', fix: 'Share with its people by hand.', auto: false,
        open: 1, fixed: 0, lastFixed: null },
      { id: 'drive.mtime', service: 'drive', title: 'Modified time differs', cause: '', fix: '', auto: true,
        open: 0, fixed: 0, lastFixed: null },
    ],
    recent: [],
  }),
}))

import KnownIssuesPanel from './KnownIssuesPanel'

describe('known issues', () => {
  it('lists only kinds this account has had, with open and fixed counts', async () => {
    render(<KnownIssuesPanel accountId={5} />)
    expect(await screen.findByTestId('issue-gmail.labels_added')).toHaveTextContent('4329')
    expect(screen.getByTestId('issue-drive.group_grant')).toHaveTextContent('Needs a person')
    expect(screen.queryByTestId('issue-drive.mtime')).not.toBeInTheDocument()
  })

  it('says why it happens and how it is fixed', async () => {
    render(<KnownIssuesPanel accountId={5} />)
    fireEvent.click(await screen.findByLabelText(/show how Gmail added a label/))
    expect(await screen.findByText(/Remove the extra labels on the target copy/)).toBeInTheDocument()
  })
})
