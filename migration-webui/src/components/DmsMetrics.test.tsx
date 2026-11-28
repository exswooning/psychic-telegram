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

vi.mock('@/api/controlPlane', () => ({ startDms: vi.fn() }))

test('the DMS can be run again from its card, with a reason, and says it started', async () => {
  const { startDms } = await import('@/api/controlPlane')
  vi.mocked(startDms).mockResolvedValue({ ok: true, actionId: 1, detail: 'started pid 42' })
  vi.mocked(fetchDmsMetrics).mockResolvedValue({ ageSeconds: 1, data: {
    status: 'complete', metrics: { 'Emails imported': 10, 'Emails discovered': 10 }, read_at: 300 } })
  const { fireEvent } = await import('@testing-library/react')
  render(<DmsMetrics accountId={3} />)
  fireEvent.click(await screen.findByTestId('dms-start'))
  const reason = await screen.findByRole('textbox')
  fireEvent.change(reason, { target: { value: 'cutover final mail' } })
  fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
  expect(await screen.findByTestId('dms-start-result')).toHaveTextContent('started pid 42')
  expect(startDms).toHaveBeenCalledWith('cutover final mail', 3)
})

test('the time left is shown from the import pace, and not at all without one', async () => {
  const data = { status: 'In progress', metrics: { 'Emails imported': 1900, 'Emails discovered': 10000 }, read_at: 300 }
  vi.mocked(fetchDmsMetrics).mockResolvedValue({ ageSeconds: 1, data,
    eta: { done: false, total: 10000, remaining: 8100, perHour: 1800, etaSeconds: 16200, basedOnMinutes: 60, asOf: 300 } })
  const { unmount } = render(<DmsMetrics accountId={3} />)
  expect(await screen.findByTestId('dms-eta')).toHaveTextContent('About 4 h 30 min left at 1,800 emails an hour')
  expect(screen.getByText(/1,900 of 10,000 emails imported/)).toBeInTheDocument()
  unmount()
  vi.mocked(fetchDmsMetrics).mockResolvedValue({ ageSeconds: 1, data, eta: null })
  render(<DmsMetrics accountId={3} />)
  expect(await screen.findByText(/1,900 of 10,000 emails imported/)).toBeInTheDocument()
  expect(screen.queryByTestId('dms-eta')).not.toBeInTheDocument()
})

test('the bar measures against the source count when the console counts as it goes', async () => {
  vi.mocked(fetchDmsMetrics).mockResolvedValue({ ageSeconds: 1,
    data: { status: 'In progress', metrics: { 'Emails imported': 2800, 'Emails discovered': 2800 }, read_at: 300 },
    eta: { done: false, total: 11553, remaining: 8753, perHour: 1800, etaSeconds: 17506, basedOnMinutes: 60 } })
  render(<DmsMetrics accountId={3} />)
  expect(await screen.findByText(/2,800 of about 11,553 \(found on the source\) emails imported/)).toBeInTheDocument()
  expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', String(Math.round(100 * 2800 / 11553)))
})
