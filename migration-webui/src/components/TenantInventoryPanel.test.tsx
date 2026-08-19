import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TenantInventoryPanel } from './TenantInventoryPanel'
import type { TenantInventory } from '@/api/controlPlane'

/**
 * The panel that answers "did I point at the right tenant, and how big is
 * it". These pin the honesty rules, not the layout.
 *
 * The one that matters: a per-account probe can fail for entirely ordinary
 * reasons (a suspended or never-provisioned mailbox answers 400/401 -- one
 * such account was found on a real 201-account tenant), so the totals are
 * often built from fewer accounts than the tenant has. A total that does
 * not say what it covers reads as the whole tenant and the reader cannot
 * tell the difference. `covered` is that denominator and it has to be on
 * screen whenever it is not the full headcount.
 */
const inv = (over: Partial<TenantInventory> = {}): TenantInventory => ({
  side: 'source',
  domain: 'src.example.com',
  accounts: 3,
  users: [
    { email: 'a@x.com', messages: 10, threads: 4, driveBytes: 1024, error: '' },
    { email: 'b@x.com', messages: 20, threads: 8, driveBytes: 2048, error: '' },
    { email: 'c@x.com', messages: null, threads: null, driveBytes: null,
      error: 'gmail: HttpError 400; drive: HttpError 401' },
  ],
  totals: { messages: 30, threads: 12, driveBytes: 3072, covered: 2 },
  truncated: false,
  error: '',
  ...over,
})

describe('TenantInventoryPanel', () => {
  it('shows the headcount and the data totals', () => {
    render(<TenantInventoryPanel inv={inv()} busy={false} error=""
                                 domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.getByTestId('stat-accounts')).toHaveTextContent('3')
    expect(screen.getByTestId('stat-messages')).toHaveTextContent('30')
    expect(screen.getByTestId('stat-drive')).toHaveTextContent('3.0 KB')
  })

  it('names the denominator when the totals do not cover every account', () => {
    render(<TenantInventoryPanel inv={inv()} busy={false} error=""
                                 domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.getByTestId('coverage-note'))
      .toHaveTextContent('Totals cover 2 of 3 accounts')
  })

  it('says nothing about coverage when every account was read', () => {
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ accounts: 2,
                 users: inv().users.slice(0, 2),
                 totals: { messages: 30, threads: 12, driveBytes: 3072, covered: 2 } })} />)
    expect(screen.queryByTestId('coverage-note')).toBeNull()
  })

  it('renders an unread account as "—", never as zero', () => {
    /* A real measured zero and "could not read" are different facts. A
       never-provisioned mailbox rendering as 0 messages is indistinguishable
       from an empty one. */
    render(<TenantInventoryPanel inv={inv()} busy={false} error=""
                                 domain="src.example.com" onRefresh={() => {}} />)
    const row = screen.getByTestId('row-c@x.com')
    expect(row).toHaveTextContent('—')
    expect(row).toHaveTextContent('could not read')
    expect(row).not.toHaveTextContent('0')
  })

  it('surfaces a tenant-level failure instead of an empty table', () => {
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ users: [], accounts: 0, error: 'SOURCE_ADMIN is not set' })} />)
    expect(screen.getByText(/SOURCE_ADMIN is not set/)).toBeTruthy()
  })

  it('reports a fetch failure without pretending the tenant is empty', () => {
    render(<TenantInventoryPanel inv={null} busy={false} error="network down"
                                 domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.getByText(/network down/)).toBeTruthy()
    expect(screen.queryByTestId('stat-accounts')).toBeNull()
  })

  it('says when the row list is truncated so the table is not read as the whole tenant', () => {
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ accounts: 500, truncated: true })} />)
    expect(screen.getByTestId('coverage-note')).toHaveTextContent('truncated')
  })

  it('scales bytes rather than printing raw counts', () => {
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ totals: { messages: 1, threads: 1,
                           driveBytes: 60_713_000_000, covered: 1 } })} />)
    expect(screen.getByTestId('stat-drive')).toHaveTextContent('GB')
  })
})
