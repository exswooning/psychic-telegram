/**
 * The Setup Wizard's front door.
 *
 * It used to open on "Seed a test tenant / Set up for a real migration" --
 * a choice about MODE, before anyone had said which tenant they meant. So
 * the domain got asked for later, separately, inside whichever panel the
 * mode led to, and a migration asked for its two domains in two different
 * places that never mentioned each other.
 *
 * Now the tenant is the first question and the purpose is the second, and
 * a migration is told up front that it needs a second domain.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Wizard, { looksLikeDomain } from './Wizard'

const seedEnabled = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => seedEnabled(),
}))
vi.mock('@/pages/SeedWizard', () => ({
  default: ({ sourceDomain }: { sourceDomain?: string }) =>
    <div data-testid="seed-wizard">seeding {sourceDomain}</div>,
}))
vi.mock('@/components/QuickTenantSetup', () => ({
  default: ({ side, initialDomain }: { side: string; initialDomain?: string }) =>
    <div data-testid={`qts-${side}`}>{initialDomain}</div>,
}))
vi.mock('@/components/JobRunner', () => ({ default: () => null }))
vi.mock('@/api/client', () => ({
  fetchStatus: () => Promise.resolve({ steps: [] }),
  fetchActions: () => Promise.resolve({}),
  fetchConfig: () => Promise.resolve({ fields: {} }),
  fetchDwd: () => Promise.resolve({}),
  saveConfig: () => Promise.resolve({ ok: true }),
  setRunMode: () => Promise.resolve({ ok: true }),
  checkStep: () => Promise.resolve({ ok: true }),
  uploadCredential: () => Promise.resolve({ ok: true }),
  checkDwdNow: () => Promise.resolve({}),
  diagnoseScopes: () => Promise.resolve({}),
}))

beforeEach(() => {
  seedEnabled.mockResolvedValue({ seed_enabled: true })
})

const view = () => render(<MemoryRouter><Wizard /></MemoryRouter>)

const enterDomain = async (d: string) => {
  const box = await screen.findByTestId('wizard-domain')
  fireEvent.change(box, { target: { value: d } })
  fireEvent.click(screen.getByTestId('wizard-domain-next'))
}

describe('the tenant is the first question', () => {
  it('opens asking for a domain, not for a mode', async () => {
    view()
    expect(await screen.findByTestId('wizard-domain')).toBeInTheDocument()
    expect(screen.queryByTestId('purpose-seed')).toBeNull()
  })

  it('will not continue on an empty field', async () => {
    view()
    await screen.findByTestId('wizard-domain')
    expect(screen.getByTestId('wizard-domain-next')).toBeDisabled()
  })

  it('will not continue on something that is not a domain', async () => {
    view()
    fireEvent.change(await screen.findByTestId('wizard-domain'),
                     { target: { value: 'not a domain' } })
    expect(screen.getByTestId('wizard-domain-next')).toBeDisabled()
  })

  it('says why, rather than just staying disabled', async () => {
    view()
    fireEvent.change(await screen.findByTestId('wizard-domain'),
                     { target: { value: 'acme' } })
    expect(screen.getByText(/does not look like a domain/)).toBeInTheDocument()
  })

  it('does not complain before anything has been typed', async () => {
    view()
    await screen.findByTestId('wizard-domain')
    expect(screen.queryByText(/does not look like a domain/)).toBeNull()
  })
})

describe('then what the tenant is for', () => {
  it('offers seed and migrate once a domain is known', async () => {
    view()
    await enterDomain('acme.com')
    expect(await screen.findByTestId('purpose-seed')).toBeInTheDocument()
    expect(screen.getByTestId('purpose-migrate')).toBeInTheDocument()
  })

  it('names the tenant being decided about', async () => {
    view()
    await enterDomain('acme.com')
    expect(await screen.findByRole('heading', { name: 'acme.com' }))
      .toBeInTheDocument()
  })

  it('hides seeding from an account not entitled to it', async () => {
    seedEnabled.mockResolvedValue({ seed_enabled: false })
    view()
    await enterDomain('acme.com')
    await screen.findByTestId('purpose-migrate')
    expect(screen.queryByTestId('purpose-seed')).toBeNull()
  })
})

describe('seeding goes straight to work', () => {
  it('does not ask for a second domain', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    expect(await screen.findByTestId('seed-wizard')).toBeInTheDocument()
  })

  it('carries the domain into the setup panel', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    expect(await screen.findByTestId('seed-wizard'))
      .toHaveTextContent('seeding acme.com')
  })
})

describe('migrating asks where it is going', () => {
  it('asks for a second domain', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-migrate'))
    expect(await screen.findByText(/Migrate acme\.com into/)).toBeInTheDocument()
  })

  it('refuses the same domain on both sides', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-migrate'))
    fireEvent.change(await screen.findByTestId('wizard-domain'),
                     { target: { value: 'acme.com' } })
    expect(screen.getByTestId('wizard-domain-next')).toBeDisabled()
    expect(screen.getByText(/same tenant/)).toBeInTheDocument()
  })

  it('says which side is read and which is written', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-migrate'))
    expect(await screen.findByText(/read, never written/)).toBeInTheDocument()
  })

  it('hands each domain to its own side', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-migrate'))
    await enterDomain('newco.com')
    await waitFor(() =>
      expect(screen.getByTestId('qts-source')).toHaveTextContent('acme.com'))
    expect(screen.getByTestId('qts-target')).toHaveTextContent('newco.com')
  })
})

describe('going back', () => {
  it('can change the tenant after choosing a purpose', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(await screen.findByTestId('wizard-change'))
    expect(await screen.findByTestId('wizard-domain')).toBeInTheDocument()
  })

  it('remembers what was typed rather than making it be retyped', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(await screen.findByTestId('wizard-change'))
    expect(await screen.findByTestId('wizard-domain')).toHaveValue('acme.com')
  })
})

describe('looksLikeDomain', () => {
  it('accepts real ones', () => {
    for (const d of ['acme.com', 'source.rohitrokaya.com.np', 'a-b.co.uk'])
      expect(looksLikeDomain(d)).toBe(true)
  })
  it('rejects the near misses people actually type', () => {
    for (const d of ['acme', 'admin@acme.com', 'acme.', '.acme.com',
                     'two words.com', 'https://acme.com', ''])
      expect(looksLikeDomain(d)).toBe(false)
  })
})
