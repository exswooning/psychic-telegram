import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

/* Live: Overview showed gorkhatweed.com's numbers while "Viewing account" read s. -> t. */

const cp = vi.hoisted(() => ({ fetchMe: vi.fn(), fetchMigrations: vi.fn() }))
vi.mock('@/api/controlPlane', () => cp)
vi.mock('@/api/migration', () => ({ chooseMigration: vi.fn() }))

import ScopeMismatch from './ScopeMismatch'

const rows = [
  { accountId: 66, sourceDomain: 'gorkhatweed.com', targetDomain: 'new.gorkhatweed.com', accountName: 'a' },
  { accountId: 3, sourceDomain: 's.example', targetDomain: 't.example', accountName: 'b' },
]
const at = (url: string) => render(<MemoryRouter initialEntries={[url]}><ScopeMismatch /></MemoryRouter>)

beforeEach(() => {
  cp.fetchMe.mockResolvedValue({ id: 66, migration_id: 66, is_superadmin: true })
  cp.fetchMigrations.mockResolvedValue({ migrations: rows })
})

describe('the header says which account the page is about', () => {
  it('warns when Viewing account differs from the migration the page loaded', async () => {
    at('/overview?account=3')
    const banner = await screen.findByTestId('scope-mismatch')
    expect(banner.textContent).toContain('gorkhatweed.com → new.gorkhatweed.com')
    expect(banner.textContent).toContain('s.example → t.example')
    expect(screen.getByRole('button', { name: /Show s\.example → t\.example/ })).toBeTruthy()
  })

  it('stays quiet when they agree', async () => {
    at('/overview?account=66')
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByTestId('scope-mismatch')).toBeNull()
  })

  it('stays quiet with no account chosen', async () => {
    at('/overview')
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByTestId('scope-mismatch')).toBeNull()
  })
})
