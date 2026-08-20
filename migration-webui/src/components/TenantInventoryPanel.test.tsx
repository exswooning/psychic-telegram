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
    { email: 'a@x.com', emails: 10, threads: 4, driveBytes: 1024,
      license: 'Business Standard', error: '' },
    { email: 'b@x.com', emails: 20, threads: 8, driveBytes: 2048,
      license: 'Business Starter', error: '' },
    { email: 'c@x.com', emails: null, threads: null, driveBytes: null,
      license: '', error: 'gmail: HttpError 400; drive: HttpError 401' },
  ],
  totals: { emails: 30, threads: 12, driveBytes: 3072, covered: 2 },
  truncated: false,
  error: '',
  deep: false,
  deepSampled: 0,
  licenseCounts: { 'Business Standard': 1, 'Business Starter': 1 },
  licenseError: '',
  ...over,
})

describe('TenantInventoryPanel', () => {
  it('shows the headcount and the data totals', () => {
    render(<TenantInventoryPanel inv={inv()} busy={false} error=""
                                 domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.getByTestId('stat-accounts')).toHaveTextContent('3')
    expect(screen.getByTestId('stat-emails')).toHaveTextContent('30')
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
                 totals: { emails: 30, threads: 12, driveBytes: 3072, covered: 2 } })} />)
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
      inv={inv({ totals: { emails: 1, threads: 1,
                           driveBytes: 60_713_000_000, covered: 1 } })} />)
    expect(screen.getByTestId('stat-drive')).toHaveTextContent('GB')
  })
})

describe('TenantInventoryPanel — licences and deep scan', () => {
  it('shows each plan with its headcount', () => {
    render(<TenantInventoryPanel inv={inv()} busy={false} error=""
                                 domain="src.example.com" onRefresh={() => {}} />)
    const row = screen.getByTestId('licence-row')
    expect(row).toHaveTextContent('Business Standard · 1')
    expect(row).toHaveTextContent('Business Starter · 1')
  })

  it('says licences could not be read rather than showing none', () => {
    /* "we could not read licences" and "this tenant has no licences" are
       opposite facts. The scope is one most tenants have never granted, so
       the empty case is the common one and must not lie. */
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ licenseCounts: {},
                 licenseError: 'needs the .../apps.licensing scope' })} />)
    expect(screen.getByTestId('licence-unavailable'))
      .toHaveTextContent('not available — needs the .../apps.licensing scope')
  })

  it('labels mailbox items "email", not "messages"', () => {
    /* Chat also has messages; the ambiguity was the point of the rename. */
    render(<TenantInventoryPanel inv={inv()} busy={false} error=""
                                 domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.getByTestId('stat-emails')).toHaveTextContent('email')
    expect(screen.getByTestId('stat-emails')).not.toHaveTextContent('messages')
  })

  it('hides sharing columns until a deep scan has actually run', () => {
    /* Rendering 0 shared files for a scan that never looked would be a
       measurement nobody took. */
    render(<TenantInventoryPanel inv={inv()} busy={false} error=""
                                 domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.queryByTestId('stat-shared')).toBeNull()
    expect(screen.queryByTestId('stat-external')).toBeNull()
  })

  it('shows the sharing facts once a deep scan has run', () => {
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ deep: true, deepSampled: 3,
                 totals: { emails: 30, threads: 12, driveBytes: 3072, covered: 2,
                           shared: 9, external: 4, anyone: 2, calendarEvents: 51,
                           driveKinds: { document: 12, spreadsheet: 3 } } })} />)
    expect(screen.getByTestId('stat-shared')).toHaveTextContent('9')
    expect(screen.getByTestId('stat-external')).toHaveTextContent('4')
    expect(screen.getByTestId('stat-anyone')).toHaveTextContent('2')
    expect(screen.getByTestId('stat-events')).toHaveTextContent('51')
    expect(screen.getByTestId('drive-kinds')).toHaveTextContent('document · 12')
  })

  it('offers the deep scan only while it has not been run', () => {
    const { rerender } = render(
      <TenantInventoryPanel inv={inv()} busy={false} error=""
                            domain="src.example.com" onRefresh={() => {}}
                            onDeepScan={() => {}} />)
    expect(screen.getByTestId('deep-scan')).toBeTruthy()
    rerender(<TenantInventoryPanel inv={inv({ deep: true })} busy={false} error=""
                                   domain="src.example.com" onRefresh={() => {}}
                                   onDeepScan={() => {}} />)
    expect(screen.queryByTestId('deep-scan')).toBeNull()
  })
})

describe('TenantInventoryPanel — deep scan honesty', () => {
  it('says the sharing figures came from a sample, not the tenant', () => {
    /* One account's Drive took ~180s to walk on a real 201-account tenant,
       so the panel samples. Rendering a 3-account sum beside a headcount of
       201 without saying so invites reading it as the whole tenant. */
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ deep: true, deepSampled: 3, accounts: 201 })} />)
    expect(screen.getByTestId('sample-note'))
      .toHaveTextContent('from 3 of 201 accounts')
  })

  it('says nothing about sampling when the scan covered everyone', () => {
    render(<TenantInventoryPanel busy={false} error="" domain="src.example.com"
                                 onRefresh={() => {}}
      inv={inv({ deep: true, deepSampled: 3, accounts: 3 })} />)
    expect(screen.queryByTestId('sample-note')).toBeNull()
  })
})

describe('deep data must not be overwritten by a shallower read', () => {
  /**
   * The quick read and the stored deep scan are fetched together and race.
   * The stored scan answers in milliseconds; the quick read walks every
   * account and takes ~30 seconds on a 201-account tenant -- so it lands
   * LAST and overwrote a complete scan. Sharing, Chat and calendar columns
   * were present one moment and gone the next.
   *
   * The rule lives in the parent's applyInv, so this pins the property the
   * panel depends on: a rendered deep snapshot has columns a quick one does
   * not, and losing them is a visible regression, not a cosmetic one.
   */
  const deep = () => inv({
    deep: true, deepSampled: 3,
    totals: { emails: 30, threads: 12, driveBytes: 3072, covered: 2,
              shared: 9, external: 4, anyone: 2, calendarEvents: 51,
              chatMessages: 7, chatSpaces: 2, driveKinds: { document: 12 } },
  })

  it('a deep snapshot renders the columns a quick one cannot', () => {
    const { rerender } = render(
      <TenantInventoryPanel inv={inv()} busy={false} error=""
                            domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.queryByTestId('stat-shared')).toBeNull()

    rerender(<TenantInventoryPanel inv={deep()} busy={false} error=""
                                   domain="src.example.com" onRefresh={() => {}} />)
    expect(screen.getByTestId('stat-shared')).toHaveTextContent('9')
    expect(screen.getByTestId('stat-chat')).toHaveTextContent('7')
    expect(screen.getByTestId('stat-spaces')).toHaveTextContent('2')
    expect(screen.getByTestId('stat-events')).toHaveTextContent('51')
  })

  it('shows scan progress instead of a bare spinner while running', () => {
    /* A 200-account scan that says nothing for an hour is indistinguishable
       from one that has died -- and they have died. */
    render(<TenantInventoryPanel inv={inv()} busy error=""
                                 domain="src.example.com" onRefresh={() => {}}
                                 scanProgress={{ done: 47, total: 201 }} />)
    expect(screen.getByTestId('scan-progress'))
      .toHaveTextContent('scanning 47 of 201 accounts')
  })

  it('falls back to a plain message when there is no count yet', () => {
    render(<TenantInventoryPanel inv={inv()} busy error=""
                                 domain="src.example.com" onRefresh={() => {}}
                                 scanProgress={{ done: 0, total: 0 }} />)
    expect(screen.getByTestId('scan-progress'))
      .toHaveTextContent('reading the tenant')
  })
})
