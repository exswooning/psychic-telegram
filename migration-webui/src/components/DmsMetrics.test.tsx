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
