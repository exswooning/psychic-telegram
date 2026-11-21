import React from 'react'
import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import DmsUserProgress from './DmsUserProgress'
import { fetchDmsPerUser } from '@/api/controlPlane'

vi.mock('@/api/controlPlane', () => ({ fetchDmsPerUser: vi.fn() }))

test('a bar per user, least done first, and an unreadable mailbox says so', async () => {
  vi.mocked(fetchDmsPerUser).mockResolvedValue({
    at: 1794940000, users: [
      { source: 'done@a', target: 'done@b', status: 'DONE', sourceCount: 40, targetCount: 42, share: 1 },
      { source: 'half@a', target: 'half@b', status: 'AWAITING_DMS', sourceCount: 100, targetCount: 50, share: 0.5 },
      { source: 'gone@a', target: 'gone@b', status: 'AWAITING_DMS', sourceCount: 5, targetCount: null, share: null },
    ],
  })
  render(<DmsUserProgress accountId={5} />)
  expect(await screen.findByText('50 of 100')).toBeTruthy()
  expect(screen.getByText('could not be read')).toBeTruthy()
  expect(screen.getAllByRole('progressbar').map((b) => b.getAttribute('aria-label'))).toEqual([
    'gone@a: unknown of its mail on the target', 'half@a: 50% of its mail on the target',
    'done@a: 100% of its mail on the target'])
})
