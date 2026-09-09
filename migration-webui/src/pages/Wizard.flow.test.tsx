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

/** Radio, then Continue -- the same two beats as Google Workspace signup's
 *  "Number of employees" step, which this flow is modelled on. */
const choose = async (purpose: 'seed' | 'migrate') => {
  fireEvent.click(await screen.findByTestId(`purpose-${purpose}`))
  fireEvent.click(screen.getByTestId('purpose-next'))
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
    await choose('seed')
    expect(await screen.findByTestId('seed-wizard')).toBeInTheDocument()
  })

  it('carries the domain into the setup panel', async () => {
    view()
    await enterDomain('acme.com')
    await choose('seed')
    expect(await screen.findByTestId('seed-wizard'))
      .toHaveTextContent('seeding acme.com')
  })
})

describe('migrating asks where it is going', () => {
  it('asks for a second domain', async () => {
    view()
    await enterDomain('acme.com')
    await choose('migrate')
    expect(await screen.findByRole('heading', { name: /where is it going/i }))
      .toBeInTheDocument()
    expect(screen.getByLabelText(/destination domain/i)).toBeInTheDocument()
  })

  it('still names the source it is migrating away from', async () => {
    view()
    await enterDomain('acme.com')
    await choose('migrate')
    expect(await screen.findByText(/acme\.com is the source/)).toBeInTheDocument()
  })

  it('refuses the same domain on both sides', async () => {
    view()
    await enterDomain('acme.com')
    await choose('migrate')
    fireEvent.change(await screen.findByTestId('wizard-domain'),
                     { target: { value: 'acme.com' } })
    expect(screen.getByTestId('wizard-domain-next')).toBeDisabled()
    expect(screen.getByText(/same tenant/)).toBeInTheDocument()
  })

  it('says which side is read and which is written', async () => {
    view()
    await enterDomain('acme.com')
    await choose('migrate')
    expect(await screen.findByText(/read, never written/)).toBeInTheDocument()
  })

  it('hands each domain to its own side', async () => {
    view()
    await enterDomain('acme.com')
    await choose('migrate')
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
    await choose('seed')
    fireEvent.click(await screen.findByTestId('wizard-change'))
    expect(await screen.findByTestId('wizard-domain')).toBeInTheDocument()
  })

  it('remembers what was typed rather than making it be retyped', async () => {
    view()
    await enterDomain('acme.com')
    await choose('seed')
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


describe('it reads like the Google Workspace signup it sits beside', () => {
  it('leads with one large heading, not a section title', async () => {
    /* The page used an h4 at 1.5rem, which reads as a settings pane rather
       than the front door of a setup. */
    view()
    const h = await screen.findByRole('heading', { name: /let's get started/i })
    expect(h.tagName).toBe('H1')
  })

  it('asks exactly one question at a time', async () => {
    view()
    await screen.findByTestId('wizard-domain')
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
  })

  it('offers the choice as radios, the way the signup does', async () => {
    view()
    await enterDomain('acme.com')
    const radios = await screen.findAllByRole('radio')
    expect(radios).toHaveLength(2)
  })

  it('will not continue until one is picked', async () => {
    view()
    await enterDomain('acme.com')
    expect(await screen.findByTestId('purpose-next')).toBeDisabled()
    fireEvent.click(screen.getByTestId('purpose-seed'))
    expect(screen.getByTestId('purpose-next')).toBeEnabled()
  })
})

describe('the panel beside the form says something true', () => {
  /* Google puts marketing there. This carries what the wizard is about to
     do -- and has to actually change with the answer, or it is just a
     differently-shaped ornament. */
  it('says what setting up a domain gets you', async () => {
    view()
    expect(await screen.findByText(/own throwaway Cloud project/i))
      .toBeInTheDocument()
  })

  it('promises nothing is created yet, because nothing is', async () => {
    view()
    expect(await screen.findByText(/Nothing is created until you confirm/i))
      .toBeInTheDocument()
  })

  it('changes when the purpose is picked', async () => {
    view()
    await enterDomain('acme.com')
    await screen.findByTestId('purpose-seed')
    expect(screen.getByText(/Rehearse it, or run it/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('purpose-migrate'))
    expect(await screen.findByText(/only ever reads the source/i))
      .toBeInTheDocument()
  })

  it('names the tenant it is talking about', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    expect(await screen.findByText(/written into\s+acme\.com/i)).toBeInTheDocument()
  })

  it('states the read-only guarantee on the migrate path', async () => {
    /* The single most important fact about pointing this at a real tenant.
       The artwork carries direction; this sentence carries the promise. */
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-migrate'))
    expect(await screen.findByText(/physically cannot write to it/i))
      .toBeInTheDocument()
  })
})

describe('the panel is designed, not annotated', () => {
  /* It began as a labelled schematic -- tenant boxes, arrows, captions --
     which read as a figure lifted out of documentation on the first screen
     of a setup. The meaning lives in the composition now; the words live
     under it. */
  const art = () => document.querySelector('svg[role="img"]')

  it('shows artwork on the first step', async () => {
    view()
    await screen.findByTestId('wizard-domain')
    expect(art()).toBeTruthy()
  })

  it('carries no labels inside the drawing', async () => {
    view()
    await screen.findByTestId('wizard-domain')
    expect(art()!.querySelectorAll('text')).toHaveLength(0)
  })

  it('still describes itself for a reader who cannot see it', async () => {
    view()
    await screen.findByTestId('wizard-domain')
    expect(art()!.getAttribute('aria-label')).toMatch(/tenant/i)
  })

  it('changes with the choice rather than being one static picture', async () => {
    view()
    await enterDomain('acme.com')
    fireEvent.click(await screen.findByTestId('purpose-migrate'))
    await waitFor(() =>
      expect(art()!.getAttribute('aria-label')).toMatch(/one direction only/i))
    fireEvent.click(screen.getByTestId('purpose-seed'))
    await waitFor(() =>
      expect(art()!.getAttribute('aria-label')).toMatch(/falling into a single tenant/i))
  })

  it('scales with its column instead of overflowing it', async () => {
    view()
    await screen.findByTestId('wizard-domain')
    expect(art()!.getAttribute('viewBox')).toBeTruthy()
    expect(art()!.getAttribute('width')).toBe('100%')
  })

  it('gives its gradients unique ids per variant, so two cannot collide', async () => {
    /* Every SVG on a page shares one id namespace: a fixed "grad" would
       mean the second illustration silently renders with the first one's
       fill. */
    view()
    await screen.findByTestId('wizard-domain')
    const ids = [...art()!.querySelectorAll('[id]')].map((n) => n.id)
    expect(ids.every((i) => i.startsWith('wa-setup'))).toBe(true)
  })
})
