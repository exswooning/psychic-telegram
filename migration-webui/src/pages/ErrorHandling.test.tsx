import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

/* A failed run can leave thousands of rows: the page drew them all, the filter had only a
   placeholder for a name, and the rows opened a record on click that a keyboard could not reach. */

const cp = vi.hoisted(() => ({ fetchFailures: vi.fn() }))
vi.mock('@/api/controlPlane', () => cp)
vi.mock('@/components/ForensicModal', () => ({
  default: ({ open, sourceUser }: { open: boolean; sourceUser: string | null }) =>
    open ? <div data-testid="forensic">{sourceUser}</div> : null,
}))

import ErrorHandling from './ErrorHandling'

const rows = (n: number) => Array.from({ length: n }, (_, i) => ({
  id: i + 1, source_user: `u${i}@s.example`, item_id: `i${i}`, item_type: 'file', status: 'FAILED',
  error_message: `boom ${i}`, timestamp: '2026-11-01T00:00:00Z',
}))

beforeEach(() => { cp.fetchFailures.mockReset() })

describe('the failures list', () => {
  it('draws a page of rows and offers the rest', async () => {
    cp.fetchFailures.mockResolvedValue(rows(450))
    render(<ErrorHandling />)
    const more = await screen.findByTestId('show-more-failures')
    expect(more.textContent).toContain('200 of 450')
    expect(screen.getAllByRole('row').length).toBe(201)       // header + 200
    fireEvent.click(more)
    expect(screen.getAllByRole('row').length).toBe(401)
    fireEvent.click(screen.getByTestId('show-more-failures'))
    expect(screen.queryByTestId('show-more-failures')).toBeNull()
    expect(screen.getAllByRole('row').length).toBe(451)
  })

  it('names the filter for a screen reader', async () => {
    cp.fetchFailures.mockResolvedValue(rows(3))
    render(<ErrorHandling />)
    expect(await screen.findByLabelText('Filter failures by user, type or message')).toBeTruthy()
  })

  it('opens a record from the keyboard', async () => {
    cp.fetchFailures.mockResolvedValue(rows(3))
    render(<ErrorHandling />)
    const row = await screen.findByLabelText('Open the record for file of u1@s.example')
    expect(row.getAttribute('tabindex')).toBe('0')
    fireEvent.keyDown(row, { key: 'Enter' })
    expect((await screen.findByTestId('forensic')).textContent).toBe('u1@s.example')
  })
})
