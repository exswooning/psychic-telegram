import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

/* 8 Nov 2026: the page showed account 3 and the import ran for account 2, the header's migration picker --
   the button never said which account. It says the one the page is viewing, and so does its status feed. */
const client = vi.hoisted(() => ({
  runAction: vi.fn(async () => ({ ok: true, error: null })),
  fetchDmsStatus: vi.fn(async () => ({ running: false, lines: [] })),
}))
vi.mock('@/api/client', () => client)

import DmsImportButton from './DmsImportButton'

beforeEach(() => { client.runAction.mockClear(); client.fetchDmsStatus.mockClear() })

describe('the DMS import button', () => {
  it('starts the import for the account the page is viewing, and watches that account', async () => {
    render(<DmsImportButton spec={{ label: 'Start Google DMS mail import', confirm: 'DMS' } as never} accountId={3} />)
    fireEvent.click(screen.getByTestId('action-dms_import'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'DMS' } })
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    await waitFor(() => expect(client.runAction).toHaveBeenCalledWith('dms_import', 'DMS', 3))
    await waitFor(() => expect(client.fetchDmsStatus).toHaveBeenCalledWith(0, 3))
  })
})
