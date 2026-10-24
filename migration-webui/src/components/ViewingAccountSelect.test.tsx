import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import ViewingAccountSelect from './ViewingAccountSelect'

const me = vi.hoisted(() => ({ superadmin: true }))
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => Promise.resolve({ id: 1, is_superadmin: me.superadmin }),
  fetchAdminAccounts: () => Promise.resolve([
    { id: 1, email: 'admin@bitport.local', source_domain: null, target_domain: null },
    { id: 7, email: 'c@x.com', source_domain: 's.com', target_domain: 't.com' }]),
}))

const Where = () => <div data-testid="where">{useLocation().search}</div>
const view = () => render(<MemoryRouter initialEntries={['/setup']}><ViewingAccountSelect /><Where /></MemoryRouter>)

describe('Viewing account selector', () => {
  beforeEach(() => sessionStorage.clear())
  it('is hidden for a non-superadmin', async () => {
    me.superadmin = false
    view()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByTestId('viewing-account')).toBeNull()
  })
  it('puts the chosen account in ?account=', async () => {
    me.superadmin = true
    view()
    fireEvent.mouseDown(await screen.findByRole('combobox'))
    fireEvent.click(await screen.findByText('s.com → t.com'))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('?account=7'))
  })
})
