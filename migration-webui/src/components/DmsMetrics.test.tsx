import React from 'react'
import { render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import DmsMetrics from './DmsMetrics'
import { fetchDmsMetrics } from '@/api/client'

vi.mock('@/api/client', () => ({ fetchDmsMetrics: vi.fn(), runAction: vi.fn() }))
beforeEach(() => { vi.clearAllMocks() })

test('older counters stay labelled as historical after an empty console read', async () => {
  vi.mocked(fetchDmsMetrics).mockResolvedValue({ ageSeconds: 1, data: {
    status: 'Not started', metrics: {}, read_at: 200,
    lastCounters: { status: 'In progress', metrics: { 'Emails imported': 9, 'Emails discovered': 10 }, read_at: 100 },
  } })
  render(<DmsMetrics accountId={3} />)
  expect(await screen.findByText(/9 of 10 emails imported/)).toBeInTheDocument()
  expect(screen.getByText(/these do not confirm the current status/)).toBeInTheDocument()
  expect(screen.getByText('Not started')).toBeInTheDocument()
})

test('fresh counters take precedence over any historical snapshot', async () => {
  vi.mocked(fetchDmsMetrics).mockResolvedValue({ ageSeconds: 1, data: {
    status: 'In progress', metrics: { 'Emails imported': 3, 'Emails discovered': 5 }, read_at: 300,
    lastCounters: { status: 'complete', metrics: { 'Emails imported': 10, 'Emails discovered': 10 }, read_at: 100 },
  } })
  render(<DmsMetrics accountId={3} />)
  expect(await screen.findByText(/3 of 5 emails imported/)).toBeInTheDocument()
  expect(screen.queryByText(/these do not confirm/)).not.toBeInTheDocument()
})
