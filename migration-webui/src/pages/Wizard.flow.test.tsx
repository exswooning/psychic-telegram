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
import Wizard, { looksLikeDomain, domainOf } from './Wizard'

const seedEnabled = vi.fn()
const fullSetup = vi.fn()
const setupStatus = vi.fn().mockResolvedValue({ running: false, result: null })
vi.mock('@/api/controlPlane', () => ({
  fetchMe: () => seedEnabled(),
  startFullSetup: (...a: unknown[]) => fullSetup(...a),
  fetchFullSetupStatus: (...a: unknown[]) => setupStatus(...a),
}))
vi.mock('@/pages/SeedWizard', () => ({
  default: ({ sourceDomain }: { sourceDomain?: string }) =>
    <div data-testid="seed-wizard">seeding {sourceDomain}</div>,
}))
vi.mock('@/components/QuickTenantSetup', () => ({
  default: ({ side, initialDomain }: { side: string; initialDomain?: string }) =>
    <div data-testid={`qts-${side}`}>{initialDomain}</div>,
}))
const narrowScopes = vi.fn().mockResolvedValue({ ok: true })
const removeTenant = vi.fn().mockResolvedValue({ ok: true })
vi.mock('@/components/JobRunner', () => ({ default: () => null }))
// ONE factory for this module. There were two, and the second silently
// replaced the first -- so repairConsoleSetup was declared in a mock that
// never took effect, and every test using it failed with "no export
// defined" while pointing at the wrong line.
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
  removeTenantSetup: (...a: unknown[]) => removeTenant(...a),
  repairConsoleSetup: (...a: unknown[]) => narrowScopes(...a),
}))

beforeEach(() => {
  seedEnabled.mockResolvedValue({ seed_enabled: true })
})

const view = () => render(<MemoryRouter><Wizard /></MemoryRouter>)

/** Step one is a sign-in now: the domain comes out of the email. */
const signIn = async (email: string, password = 'hunter22222') => {
  fireEvent.change(await screen.findByTestId('admin-email'),
                   { target: { value: email } })
  fireEvent.change(screen.getByTestId('admin-password'),
                   { target: { value: password } })
  fireEvent.click(screen.getByTestId('creds-next'))
}

/** Only the destination step still asks for a bare domain. */
const enterDomain = async (d: string) => {
  const box = await screen.findByTestId('wizard-domain')
  fireEvent.change(box, { target: { value: d } })
  fireEvent.click(screen.getByTestId('wizard-domain-next'))
}

/** Radio, then Continue -- the same two beats as Google Workspace signup's
 *  "Number of employees" step, which this flow is modelled on. */
const choose = async (purpose: 'seed' | 'migrate' | 'later') => {
  fireEvent.click(await screen.findByTestId(`purpose-${purpose}`))
  fireEvent.click(screen.getByTestId('purpose-next'))
}

describe('the credential is the first question', () => {
  /* It asked for a domain, then asked for the admin address on a later
     panel -- which contains the domain. Two questions, one fact, and a
     disagreement to handle when they differ. */
  it('opens asking to sign in, not for a mode', async () => {
    view()
    expect(await screen.findByTestId('admin-email')).toBeInTheDocument()
    expect(screen.queryByTestId('purpose-seed')).toBeNull()
  })

  it('will not continue on an empty form', async () => {
    view()
    await screen.findByTestId('admin-email')
    expect(screen.getByTestId('creds-next')).toBeDisabled()
  })

  it('will not continue on an address that is not one', async () => {
    view()
    fireEvent.change(await screen.findByTestId('admin-email'),
                     { target: { value: 'admin' } })
    fireEvent.change(screen.getByTestId('admin-password'),
                     { target: { value: 'x' } })
    expect(screen.getByTestId('creds-next')).toBeDisabled()
  })

  it('will not continue without a password', async () => {
    view()
    fireEvent.change(await screen.findByTestId('admin-email'),
                     { target: { value: 'admin@acme.com' } })
    expect(screen.getByTestId('creds-next')).toBeDisabled()
  })

  it('says why, rather than just staying disabled', async () => {
    view()
    fireEvent.change(await screen.findByTestId('admin-email'),
                     { target: { value: 'admin' } })
    expect(screen.getByText(/full admin address/i)).toBeInTheDocument()
  })

  it('does not complain before anything has been typed', async () => {
    view()
    await screen.findByTestId('admin-email')
    expect(screen.queryByText(/full admin address/i)).toBeNull()
  })

  it('promises the password is not kept', async () => {
    /* Said twice on purpose -- under the field, and in the panel. Handing a
       super-admin password to a web form is the moment someone hesitates. */
    view()
    await screen.findByTestId('admin-email')
    expect(screen.getAllByText(/never stored/i).length).toBeGreaterThan(0)
  })
})

describe('the domain comes out of the address', () => {
  it('derives it rather than asking twice', async () => {
    view()
    fireEvent.change(await screen.findByTestId('admin-email'),
                     { target: { value: 'admin@acme.com' } })
    expect(await screen.findByTestId('derived-domain')).toHaveTextContent('acme.com')
  })

  it('shows it before it is acted on, so a typo is visible', async () => {
    view()
    fireEvent.change(await screen.findByTestId('admin-email'),
                     { target: { value: 'admin@acme.co.uk' } })
    expect(await screen.findByTestId('derived-domain')).toHaveTextContent('acme.co.uk')
  })

  it('carries it into the purpose step', async () => {
    view()
    await signIn('admin@acme.com')
    expect(await screen.findByRole('heading', { name: 'acme.com' }))
      .toBeInTheDocument()
  })

  it('domainOf takes the last @, so a quoted local part cannot fool it', () => {
    expect(domainOf('a@b@acme.com')).toBe('acme.com')
    expect(domainOf('ADMIN@ACME.COM')).toBe('acme.com')
    expect(domainOf('nope')).toBe('')
  })
})

describe('then what the tenant is for', () => {
  it('offers seed and migrate once a domain is known', async () => {
    view()
    await signIn('admin@acme.com')
    expect(await screen.findByTestId('purpose-seed')).toBeInTheDocument()
    expect(screen.getByTestId('purpose-migrate')).toBeInTheDocument()
  })

  it('names the tenant being decided about', async () => {
    view()
    await signIn('admin@acme.com')
    expect(await screen.findByRole('heading', { name: 'acme.com' }))
      .toBeInTheDocument()
  })

  it('hides seeding from an account not entitled to it', async () => {
    seedEnabled.mockResolvedValue({ seed_enabled: false })
    view()
    await signIn('admin@acme.com')
    await screen.findByTestId('purpose-migrate')
    expect(screen.queryByTestId('purpose-seed')).toBeNull()
  })
})

describe('seeding goes straight to work', () => {
  it('does not ask for a second domain', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('seed')
    expect(await screen.findByTestId('seed-wizard')).toBeInTheDocument()
  })

  it('carries the domain into the setup panel', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('seed')
    expect(await screen.findByTestId('seed-wizard'))
      .toHaveTextContent('seeding acme.com')
  })
})

describe('migrating asks where it is going', () => {
  it('asks for a second domain', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('migrate')
    expect(await screen.findByRole('heading', { name: /where is it going/i }))
      .toBeInTheDocument()
    expect(screen.getByLabelText(/destination domain/i)).toBeInTheDocument()
  })

  it('still names the source it is migrating away from', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('migrate')
    expect(await screen.findByText(/acme\.com is the source/)).toBeInTheDocument()
  })

  it('refuses the same domain on both sides', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('migrate')
    fireEvent.change(await screen.findByTestId('wizard-domain'),
                     { target: { value: 'acme.com' } })
    expect(screen.getByTestId('wizard-domain-next')).toBeDisabled()
    expect(screen.getByText(/same tenant/)).toBeInTheDocument()
  })

  it('says which side is read and which is written', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('migrate')
    expect(await screen.findByText(/read, never written/)).toBeInTheDocument()
  })

  it('hands each domain to its own side', async () => {
    view()
    await signIn('admin@acme.com')
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
    await signIn('admin@acme.com')
    await choose('seed')
    fireEvent.click(await screen.findByTestId('wizard-change'))
    expect(await screen.findByTestId('admin-email')).toBeInTheDocument()
  })

  it('remembers the address rather than making it be retyped', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('seed')
    fireEvent.click(await screen.findByTestId('wizard-change'))
    expect(await screen.findByTestId('admin-email')).toHaveValue('admin@acme.com')
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

  it('asks for one thing at a time -- a sign-in is one thing', async () => {
    view()
    await screen.findByTestId('admin-email')
    // The password is type=password, so it is not a "textbox" role.
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
  })

  it('offers the choice as radios, the way the signup does', async () => {
    view()
    await signIn('admin@acme.com')
    const radios = await screen.findAllByRole('radio')
    expect(radios).toHaveLength(3)
  })

  it('will not continue until one is picked', async () => {
    view()
    await signIn('admin@acme.com')
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

  it('explains what the password is for, where it is asked for', async () => {
    view()
    await screen.findByTestId('admin-email')
    expect(screen.getAllByText(/used once to sign in/i).length)
      .toBeGreaterThan(0)
  })

  it('changes when the purpose is picked', async () => {
    view()
    await signIn('admin@acme.com')
    await screen.findByTestId('purpose-seed')
    expect(screen.getByText(/Rehearse it, or run it/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('purpose-migrate'))
    expect(await screen.findByText(/only ever reads the source/i))
      .toBeInTheDocument()
  })

  it('names the tenant it is talking about', async () => {
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    expect(await screen.findByText(/written into\s+acme\.com/i)).toBeInTheDocument()
  })

  it('states the read-only guarantee on the migrate path', async () => {
    /* The single most important fact about pointing this at a real tenant.
       The artwork carries direction; this sentence carries the promise. */
    view()
    await signIn('admin@acme.com')
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
    await screen.findByTestId('admin-email')
    expect(art()).toBeTruthy()
  })

  it('carries no labels inside the drawing', async () => {
    view()
    await screen.findByTestId('admin-email')
    expect(art()!.querySelectorAll('text')).toHaveLength(0)
  })

  it('still describes itself for a reader who cannot see it', async () => {
    view()
    await screen.findByTestId('admin-email')
    expect(art()!.getAttribute('aria-label')).toMatch(/tenant/i)
  })

  it('changes with the choice rather than being one static picture', async () => {
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-migrate'))
    await waitFor(() =>
      expect(art()!.getAttribute('aria-label')).toMatch(/one direction only/i))
    fireEvent.click(screen.getByTestId('purpose-seed'))
    await waitFor(() =>
      expect(art()!.getAttribute('aria-label')).toMatch(/falling into a single tenant/i))
  })

  it('scales with its column instead of overflowing it', async () => {
    view()
    await screen.findByTestId('admin-email')
    expect(art()!.getAttribute('viewBox')).toBeTruthy()
    expect(art()!.getAttribute('width')).toBe('100%')
  })

  it('gives its gradients unique ids per variant, so two cannot collide', async () => {
    /* Every SVG on a page shares one id namespace: a fixed "grad" would
       mean the second illustration silently renders with the first one's
       fill. */
    view()
    await screen.findByTestId('admin-email')
    const ids = [...art()!.querySelectorAll('[id]')].map((n) => n.id)
    expect(ids.every((i) => i.startsWith('wa-setup'))).toBe(true)
  })
})


describe('the illustration fills its frame', () => {
  /* The first version drew inside about 70% of the viewBox width and half
     its height. Scaled to the column, that rendered small with a dead band
     above the headline -- empty space that read as a mistake rather than as
     air. */
  const art = () => document.querySelector('svg[role="img"]')!

  const bounds = () => {
    const vb = art().getAttribute('viewBox')!.split(' ').map(Number)
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
    for (const r of art().querySelectorAll('rect')) {
      // Only rects that actually declare a position. The full-bleed texture
      // rect omits x/y -- and Number(null) is 0, not NaN, so the obvious
      // guard let it through and it became the bounds. These tests then
      // measured the background and passed with the artwork shrunk back to
      // its original size, which is precisely the bug they exist to catch.
      const ax = r.getAttribute('x'), ay = r.getAttribute('y')
      if (ax === null || ay === null) continue
      const x = Number(ax), y = Number(ay)
      const w = Number(r.getAttribute('width')), h = Number(r.getAttribute('height'))
      if ([x, y, w, h].some(Number.isNaN)) continue
      minX = Math.min(minX, x); maxX = Math.max(maxX, x + w)
      minY = Math.min(minY, y); maxY = Math.max(maxY, y + h)
    }
    return { vw: vb[2], vh: vb[3], w: maxX - minX, h: maxY - minY, minX, maxX }
  }

  it('uses most of the width it is given', async () => {
    view()
    await screen.findByTestId('admin-email')
    const b = bounds()
    expect(b.w / b.vw).toBeGreaterThan(0.85)
  })

  it('and most of the height, so there is no dead band under it', async () => {
    view()
    await screen.findByTestId('admin-email')
    const b = bounds()
    expect(b.h / b.vh).toBeGreaterThan(0.65)
  })

  it('sits roughly centred rather than drifting to one side', async () => {
    view()
    await screen.findByTestId('admin-email')
    const b = bounds()
    const leftGap = b.minX
    const rightGap = b.vw - b.maxX
    expect(Math.abs(leftGap - rightGap)).toBeLessThan(b.vw * 0.12)
  })

  it('lights the top edge of every card, so none reads as a hole', async () => {
    /* Dark mode specifically: a flat rectangle with no lit edge looks
       punched out of the panel rather than resting on it. */
    view()
    await screen.findByTestId('admin-email')
    const cards = art().querySelectorAll('rect[rx="18"], rect[rx="16"]')
    const edges = art().querySelectorAll('path[stroke-width="1"]')
    expect(edges.length).toBeGreaterThanOrEqual(cards.length)
  })
})


describe('setting everything up in one go', () => {
  /* full_setup already does the whole sequence -- project, APIs, service
     account, key, delegation, verify. It was only ever reached through a
     page of controls because that page predates the wizard knowing the
     domain and the credentials. It knows both by the time this button
     exists. */
  beforeEach(() => { fullSetup.mockReset(); fullSetup.mockResolvedValue({ ok: true }) })

  it('is offered once a purpose is picked', async () => {
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    expect(screen.getByTestId('purpose-auto')).toBeEnabled()
  })

  it('is not offered before one is', async () => {
    view()
    await signIn('admin@acme.com')
    expect(await screen.findByTestId('purpose-auto')).toBeDisabled()
  })

  it('sends the domain and credentials it already has', async () => {
    view()
    await signIn('admin@acme.com', 'sekrit123')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    await waitFor(() => expect(fullSetup).toHaveBeenCalled())
    const [, side, domain, email, password] = fullSetup.mock.calls[0]
    expect(side).toBe('source')
    expect(domain).toBe('acme.com')
    expect(email).toBe('admin@acme.com')
    expect(password).toBe('sekrit123')
  })

  it('runs for real, not as a dry run', async () => {
    /* dryRun defaults to true on the API. A "set everything up" that
       quietly did nothing would be the worst possible default here. */
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    await waitFor(() => expect(fullSetup).toHaveBeenCalled())
    expect(fullSetup.mock.calls[0][5]).toMatchObject({ dryRun: false })
  })

  it('warns that a 2-Step prompt cannot be answered for you', async () => {
    view()
    await signIn('admin@acme.com')
    expect(await screen.findByText(/2-Step prompt on your phone/i))
      .toBeInTheDocument()
  })

  it('surfaces a refusal instead of moving on as if it worked', async () => {
    fullSetup.mockResolvedValue({ ok: false, detail: 'capacity is full' })
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    expect(await screen.findByTestId('purpose-auto-error'))
      .toHaveTextContent(/capacity is full/)
  })

  it('stays on the choice when it failed, so it can be retried', async () => {
    fullSetup.mockResolvedValue({ ok: false, detail: 'nope' })
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    await screen.findByTestId('purpose-auto-error')
    expect(screen.getByTestId('purpose-auto')).toBeInTheDocument()
  })

  it('the step-by-step route is still there beside it', async () => {
    /* Which is what you want when a tenant is unusual, or a phase has
       already failed once. */
    view()
    await signIn('admin@acme.com')
    expect(await screen.findByTestId('purpose-next')).toBeInTheDocument()
  })
})


describe('setting up without committing to a purpose', () => {
  /* Setting a tenant up and deciding what to do with it are two decisions,
     and the wizard forced them together. The thing you most want before
     deciding is a count of what is in the tenant -- and counting needs the
     very setup the decision was gating. */
  it('is offered as a third choice', async () => {
    view()
    await signIn('admin@acme.com')
    expect(await screen.findByTestId('purpose-later')).toBeInTheDocument()
  })

  it('does not ask for a second domain', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('later')
    await waitFor(() => expect(screen.queryByTestId('wizard-domain')).toBeNull())
  })

  it('asks which side the tenant is before setting it up', async () => {
    /* It used to assume source. Live, somebody set up their DESTINATION
       through this and the source row was overwritten with the target's
       domain -- which disarms the typed-domain gate that stops a seed being
       aimed at production. */
    view()
    await signIn('admin@acme.com')
    await choose('later')
    expect(await screen.findByTestId('side-unset')).toBeInTheDocument()
    expect(screen.queryByTestId('qts-source')).toBeNull()
  })

  it('sets it up once a side is chosen', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('later')
    fireEvent.click(await screen.findByTestId('side-source'))
    await waitFor(() =>
      expect(screen.getByTestId('qts-source')).toHaveTextContent('acme.com'))
  })

  it('can set a tenant up as the destination, which it previously could not',
     async () => {
    view()
    await signIn('admin@acme.com')
    await choose('later')
    fireEvent.click(await screen.findByTestId('side-target'))
    await waitFor(() =>
      expect(screen.getByTestId('qts-target')).toHaveTextContent('acme.com'))
  })

  it('says the decision is still open', async () => {
    view()
    await signIn('admin@acme.com')
    await choose('later')
    expect(await screen.findByText(/Nothing here commits it to a seed or a migration/i))
      .toBeInTheDocument()
  })

  it('explains that a later choice reuses this setup', async () => {
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-later'))
    expect(await screen.findByText(/reuses all of it/i)).toBeInTheDocument()
  })
})

describe('emptying a tenant you have not committed to', () => {
  const reach = async () => {
    view()
    await signIn('admin@acme.com')
    await choose('later')
    fireEvent.click(await screen.findByTestId('side-source'))
    return screen.findByTestId('later-wipe')
  }

  it('is refused until a side is named', async () => {
    /* Wipe and delete-users both take a side. Defaulting it is the same
       mistake in a more expensive place. */
    view()
    await signIn('admin@acme.com')
    await choose('later')
    expect(await screen.findByTestId('later-wipe')).toBeDisabled()
    expect(screen.getByTestId('later-delete-users')).toBeDisabled()
  })

  it('offers a wipe', async () => {
    expect(await reach()).toBeInTheDocument()
  })

  it('offers deleting all users', async () => {
    await reach()
    expect(screen.getByTestId('later-delete-users')).toBeInTheDocument()
  })

  it('gates both on typing the domain', async () => {
    await reach()
    fireEvent.click(screen.getByTestId('later-delete-users'))
    expect(await screen.findByTestId('confirm-domain')).toBeInTheDocument()
    expect(screen.getByTestId('confirm-act')).toBeDisabled()
  })

  it('promises no administrator is removed', async () => {
    /* The one deletion with no cheap undo -- a Workspace address stays
       reserved for 20 days and there would be no credential left to undo
       it with. */
    await reach()
    expect(screen.getByText(/every super-admin and delegated admin is kept/i))
      .toBeInTheDocument()
  })
})


describe('choosing a purpose narrows the delegation', () => {
  /* Setup grants the union so the tenant works either way immediately.
     Choosing migrate has to REMOVE the source's write scopes: the
     read-only source is the guarantee this tool rests on, and a grant left
     wide makes it untrue with nothing on screen to say so. Live, the source
     held 25 scopes including full Gmail and admin.directory.user. */
  beforeEach(() => { narrowScopes.mockClear() })

  it('asks for the migrate scope set when migrating', async () => {
    view()
    await signIn('admin@acme.com', 'pw123456')
    await choose('migrate')
    await waitFor(() => expect(narrowScopes).toHaveBeenCalled())
    const [side, , opts] = narrowScopes.mock.calls[0]
    expect(side).toBe('source')
    expect(opts).toMatchObject({ purpose: 'migrate' })
  })

  it('asks for the seed scope set when seeding', async () => {
    view()
    await signIn('admin@acme.com', 'pw123456')
    await choose('seed')
    await waitFor(() => expect(narrowScopes).toHaveBeenCalled())
    expect(narrowScopes.mock.calls[0][2]).toMatchObject({ purpose: 'seed' })
  })

  it('does not narrow when the decision is deferred', async () => {
    /* "later" means no purpose has been chosen, and the union is what
       keeps the tenant usable either way. */
    view()
    await signIn('admin@acme.com', 'pw123456')
    await choose('later')
    await waitFor(() => expect(screen.getByTestId('side-unset')).toBeTruthy())
    expect(narrowScopes).not.toHaveBeenCalled()
  })

  it('does not touch the Chat app while narrowing', async () => {
    /* Re-running the console Chat step on every purpose choice would drive
       a browser for minutes to redo something already done. */
    view()
    await signIn('admin@acme.com', 'pw123456')
    await choose('migrate')
    await waitFor(() => expect(narrowScopes).toHaveBeenCalled())
    expect(narrowScopes.mock.calls[0][2]).toMatchObject({ chat: false })
  })
})


describe('the setup reports itself while it runs', () => {
  /* "Set everything up for me" fired and forgot: it jumped straight on, so
     a minute of provisioning looked identical to a button that had done
     nothing -- and any 2-Step prompt waiting on somebody's phone appeared
     on a panel they had not arrived at. */
  beforeEach(() => {
    fullSetup.mockResolvedValue({ ok: true })
    setupStatus.mockResolvedValue({ running: false, result: null })
  })

  it('shows a progress bar once it is running', async () => {
    setupStatus.mockResolvedValue({
      running: true, result: null, progressPct: 42,
      progressLabel: 'enabling APIs' })
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    expect(await screen.findByTestId('setup-progress')).toBeInTheDocument()
  })

  it('says which phase it is in, not just that it is busy', async () => {
    setupStatus.mockResolvedValue({
      running: true, result: null, progressPct: 42,
      progressLabel: 'enabling APIs' })
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    expect(await screen.findByText(/enabling APIs/)).toBeInTheDocument()
    expect(screen.getByText('42%')).toBeInTheDocument()
  })

  it('stays on the step instead of jumping away', async () => {
    setupStatus.mockResolvedValue({ running: true, result: null })
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    await screen.findByTestId('setup-progress')
    expect(screen.getByTestId('purpose-seed')).toBeInTheDocument()
  })

  it('surfaces a 2-Step prompt where the setup is being watched', async () => {
    /* The browser doing the signing in is headless on the server. */
    setupStatus.mockResolvedValue({
      running: true, result: null,
      challenge: 'Check your phone / tap 47 / Pixel 7' })
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    expect(await screen.findByTestId('mfa-banner')).toHaveTextContent('47')
  })

  it('offers a way on once it has finished', async () => {
    setupStatus.mockResolvedValue({
      running: false, result: { side: 'source', ok: true, phases: [] } })
    view()
    await signIn('admin@acme.com')
    fireEvent.click(await screen.findByTestId('purpose-seed'))
    fireEvent.click(screen.getByTestId('purpose-auto'))
    expect(await screen.findByTestId('setup-done')).toBeInTheDocument()
  })
})
