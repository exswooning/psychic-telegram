import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

/* 8 Nov 2026: the page showed account 3 and the import ran for account 2, the header's migration picker --
   the button never said which account. It says the one the page is viewing, and so does its status feed. */
const client = vi.hoisted(() => ({
  runAction: vi.fn(async () => ({ ok: true, error: null })),
  fetchDmsStatus: vi.fn(async () => ({ running: false, lines: [] })),
}))
vi.mock('@/api/client', () => client)
vi.mock('@/hooks/useAccountPair', () => ({ useAccountPair: (id?: number) => (id === 3 ? 's.example → t.example' : '') }))

import DmsImportButton from './DmsImportButton'

beforeEach(() => { client.runAction.mockClear(); client.fetchDmsStatus.mockClear() })

describe('the DMS import button', () => {
  it('starts the import for the account the page is viewing, and watches that account', async () => {
    render(<DmsImportButton spec={{ label: 'Start Google DMS mail import', confirm: 'DMS' } as never} accountId={3} />)
    fireEvent.click(screen.getByTestId('action-dms_import'))
    expect(screen.getByTestId('confirm-account')).toHaveTextContent('Runs for s.example → t.example')
    fireEvent.change(screen.getByRole('textbox', { name: /type dms/i }), { target: { value: 'DMS' } })
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    await waitFor(() => expect(client.runAction).toHaveBeenCalledWith('dms_import', 'DMS', 3))
    await waitFor(() => expect(client.fetchDmsStatus).toHaveBeenCalledWith(0, 3))
  })

  it('sends a pilot: only the users named, as a fresh import only when asked', async () => {
    render(<DmsImportButton spec={{ label: 'Start Google DMS mail import', confirm: 'DMS' } as never} accountId={3} />)
    fireEvent.click(screen.getByTestId('action-dms_import'))
    fireEvent.change(screen.getByTestId('dms-pilot-users'), { target: { value: 'ward7@s.example, ward8@s.example' } })
    fireEvent.click(screen.getByTestId('dms-fresh'))
    fireEvent.change(screen.getByRole('textbox', { name: /type dms/i }), { target: { value: 'DMS' } })
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    await waitFor(() => expect(client.runAction).toHaveBeenCalledWith(
      'dms_import', 'DMS', 3, { users: ['ward7@s.example', 'ward8@s.example'], fresh: true }))
  })

  it('cannot ask for a fresh import without naming users', () => {
    render(<DmsImportButton spec={{ label: 'Start Google DMS mail import', confirm: 'DMS' } as never} accountId={3} />)
    fireEvent.click(screen.getByTestId('action-dms_import'))
    expect((screen.getByTestId('dms-fresh') as HTMLInputElement).disabled).toBe(true)
  })

  it('records deliveries only for named users, with no fresh import offered', async () => {
    render(<DmsImportButton spec={{ label: 'Record DMS deliveries', confirm: 'RECORD' } as never} accountId={3} action="dms_reconcile" />)
    fireEvent.click(screen.getByTestId('action-dms_reconcile'))
    expect(screen.queryByTestId('dms-fresh')).toBeNull()
    fireEvent.change(screen.getByRole('textbox', { name: /type record/i }), { target: { value: 'RECORD' } })
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    expect(await screen.findByText('name the users to record')).toBeTruthy()
    fireEvent.change(screen.getByTestId('dms-pilot-users'), { target: { value: 'ward7@s.example' } })
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    await waitFor(() => expect(client.runAction).toHaveBeenCalledWith('dms_reconcile', 'RECORD', 3, { users: ['ward7@s.example'] }))
  })
})
