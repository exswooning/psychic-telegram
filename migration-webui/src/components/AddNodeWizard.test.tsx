/**
 * Adding a machine used to hand out one command with three placeholders in
 * it, for the one route nobody takes: driving a fresh Ubuntu box over SSH
 * from a third machine. The machine people actually want to add is the
 * laptop in front of them.
 *
 * The two commands here are shaped by things that bit a real install:
 * Windows PowerShell refuses to run a script FILE under its default
 * Restricted execution policy (so irm|iex, which is not a file), and a
 * piped script has no argv (so every setting travels in the environment).
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import AddNodeWizard, { joinCommand, removeCommand, codeCommand } from './AddNodeWizard'
import type { NodeJoinDetails } from '@/api/controlPlane'

// The component calls createJoinCode for real now; without this the module
// loads its own base-URL handling and fails at import time in jsdom.
const mint = vi.fn()
const me = vi.fn()
const admins = vi.fn()
const codeStatus = vi.fn()
vi.mock('@/api/controlPlane', () => ({
  createJoinCode: (...a: unknown[]) => mint(...a),
  fetchMe: () => me(),
  fetchAdminAccounts: () => admins(),
  fetchJoinCodeStatus: (...a: unknown[]) => codeStatus(...a),
}))

beforeEach(() => {
  me.mockReset(); admins.mockReset(); mint.mockReset(); codeStatus.mockReset()
  codeStatus.mockResolvedValue({ known: true, redeemed: false, expired: false,
                                 redeemedAt: '', redeemedFrom: '' })
  me.mockResolvedValue({ id: 7, email: 'ops@x.test', is_superadmin: true })
  admins.mockResolvedValue([
    { id: 7, email: 'ops@x.test',
      source_domain: 'source.acme.test', target_domain: 'target.acme.test' },
    { id: 68, email: 'admin@bitport.local',
      source_domain: 'source.acme.test', target_domain: 'target.acme.test' },
    { id: 4, email: 'solo@z.test', source_domain: 'only.test' },
  ])
})

const join = (over: Partial<NodeJoinDetails> = {}): NodeJoinDetails => ({
  enabled: true, token: 'real-secret-token', revealed: true,
  coordinatorUrl: 'http://192.168.1.50:81', leaseSeconds: 900, ...over,
})

describe('the command it hands out', () => {
  it('does not run a .ps1 file on Windows', () => {
    /* Windows PowerShell defaults to Restricted and refuses a script file --
       hit live as "running scripts is disabled on this system". A piped
       string is not a file. */
    const c = joinCommand('windows', 'http://h:81', 'tok', 7)
    expect(c).toContain('| iex')
    expect(c).not.toMatch(/\.\\install_node\.ps1/)
  })

  it('passes settings through the environment, never as arguments', () => {
    /* A piped script has no argv to receive flags on -- and argv is
       readable by every process on the box, while this line carries a live
       credential. */
    for (const os of ['windows', 'unix'] as const) {
      const c = joinCommand(os, 'http://h:81', 'tok', 7)
      expect(c).toContain('BITPORT_COORDINATOR')
      expect(c).toContain('BITPORT_NODE_TOKEN')
      expect(c).not.toContain('--token')
      expect(c).not.toContain('-Token')
    }
  })

  it('carries the address, token and account into both forms', () => {
    for (const os of ['windows', 'unix'] as const) {
      const c = joinCommand(os, 'http://192.168.1.50:81', 'sekrit', 68)
      expect(c).toContain('http://192.168.1.50:81')
      expect(c).toContain('sekrit')
      expect(c).toContain('68')
    }
  })

  it('strips a trailing slash so the path is not doubled', () => {
    expect(joinCommand('unix', 'http://h:81/', 't', 7)).toContain("'http://h:81'")
  })

  it('leaves a readable placeholder when the token is hidden', () => {
    expect(joinCommand('unix', 'http://h:81', '', 7)).toContain('<node-token>')
  })
})

describe('the address step', () => {
  it('warns that 8090 is unreachable from another machine', () => {
    /* api_server.py binds 127.0.0.1. Every doc example said :8090 until
       this was found, and a node aimed there just gets connection refused
       from an otherwise perfectly configured box. */
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.change(screen.getByTestId('node-addr'),
                     { target: { value: 'http://192.168.1.50:8090' } })
    expect(screen.getByTestId('addr-loopback')).toBeInTheDocument()
  })

  it('warns that localhost means the new machine, not this one', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.change(screen.getByTestId('node-addr'),
                     { target: { value: 'http://localhost:81' } })
    expect(screen.getByTestId('addr-localhost')).toBeInTheDocument()
  })

  it('is clean for a normal LAN address', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    expect(screen.queryByTestId('addr-loopback')).toBeNull()
    expect(screen.queryByTestId('addr-localhost')).toBeNull()
  })

  it('prefills what the server reported', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    expect(screen.getByTestId('node-addr')).toHaveValue('http://192.168.1.50:81')
  })
})

describe('choosing the OS', () => {
  it('rewrites the command in place', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    expect(screen.getByTestId('join-command')).toHaveTextContent('curl')
    fireEvent.click(screen.getByTestId('os-windows'))
    expect(screen.getByTestId('join-command')).toHaveTextContent('iex')
    expect(screen.getByTestId('join-command')).not.toHaveTextContent('curl')
  })
})

describe('the token', () => {
  it('cannot be copied while it is still hidden', () => {
    /* Copying a command with a literal <node-token> in it and running it is
       a guaranteed failure several minutes later, on the other machine. */
    render(<AddNodeWizard join={join()} revealed={false} onReveal={() => {}} />)
    expect(screen.getByTestId('copy-command')).toBeDisabled()
    expect(screen.getByTestId('token-hidden')).toBeInTheDocument()
  })

  it('is copyable once revealed', () => {
    const writeText = vi.fn()
    Object.assign(navigator, { clipboard: { writeText } })
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('copy-command'))
    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining('real-secret-token'))
  })

  it('asks the page to reveal rather than fetching it itself', () => {
    /* The reveal is a separate authenticated request the page owns; a
     * component that fetched its own would need the superadmin rule
     * duplicated in it. */
    const onReveal = vi.fn()
    render(<AddNodeWizard join={join()} revealed={false} onReveal={onReveal} />)
    fireEvent.click(screen.getByTestId('reveal-token'))
    expect(onReveal).toHaveBeenCalled()
  })
})

describe('removing it from a machine', () => {
  it('keeps the keys and the ledger by default', () => {
    /* Removing a node is routine; destroying the credentials for somebody's
       tenant is not, and the two must not share a button. */
    expect(removeCommand('unix', false)).not.toContain('--purge')
    expect(removeCommand('windows', false)).not.toContain('PURGE')
  })

  it('says so explicitly when asked to delete everything', () => {
    expect(removeCommand('unix', true)).toContain('--purge')
    expect(removeCommand('windows', true)).toContain('BITPORT_UNINSTALL_PURGE')
  })

  it('uses a form that can still carry flags', () => {
    /* `curl | bash` has no argv, which is why joining passes env vars.
       `bash -c "<script>" name args` does, so the remove command can take
       --yes and --purge directly. */
    const c = removeCommand('unix', false)
    expect(c).toContain('bash -c "$(curl')
    expect(c).toMatch(/\)" \w+ --yes/)
  })

  it('is hidden until asked for', () => {
    /* It sits under the join flow, not beside it: the page's job is adding
       machines, and a delete control at the same level invites a misclick. */
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    expect(screen.queryByTestId('purge-warning')).toBeNull()
    expect(screen.getByTestId('remove-command').closest('.MuiCollapse-root'))
      .toHaveClass('MuiCollapse-hidden')
  })

  it('opens when asked', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('toggle-remove'))
    expect(screen.getByTestId('remove-command').closest('.MuiCollapse-root'))
      .not.toHaveClass('MuiCollapse-hidden')
  })

  it('warns before handing over the destructive form', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('toggle-remove'))
    expect(screen.queryByTestId('purge-warning')).toBeNull()
    fireEvent.click(screen.getByTestId('purge-data'))
    expect(screen.getByTestId('purge-warning')).toBeInTheDocument()
    expect(screen.getByTestId('remove-command')).toHaveTextContent('--purge')
  })

  it('follows the OS picked above', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('toggle-remove'))
    expect(screen.getByTestId('remove-command')).toHaveTextContent('curl')
    fireEvent.click(screen.getByTestId('os-windows'))
    expect(screen.getByTestId('remove-command')).toHaveTextContent('iex')
  })
})

describe('joining with a code', () => {
  it('is one short line whatever the answers are', () => {
    /* The manual line carries the coordinator URL, a 43-character token and
       the account. All three live inside the code instead, so this is the
       same length every time. */
    const c = codeCommand('windows', 'https://h.example', 'B95B-RZ5Z')
    expect(c).toBe('irm https://h.example/api/v2/j/B95B-RZ5Z | iex')
    expect(c.length).toBeLessThan(60)
  })

  it('quotes the unix URL, because the query string would otherwise be eaten', () => {
    /* `?sh=true` unquoted is a shell glob, and the failure is a confusing
       "no matches found" rather than anything about joining. */
    const c = codeCommand('unix', 'https://h.example', 'B95B-RZ5Z')
    expect(c).toContain('"https://h.example/api/v2/j/B95B-RZ5Z?sh=true"')
    expect(c).toContain('| bash')
  })

  it('never doubles the slash when the origin has one', () => {
    expect(codeCommand('windows', 'https://h.example/', 'X')).toContain('example/api')
  })

  it('carries no credential of its own', () => {
    /* The whole point: this line can be read aloud, put in a chat, or
       photographed. The token is exchanged server-side, once. */
    const c = codeCommand('windows', 'https://h.example', 'B95B-RZ5Z')
    expect(c).not.toContain('BITPORT_NODE_TOKEN')
  })
})

describe('minting a code in the UI', () => {
  it('shows the code and a one-line command', async () => {
    mint.mockResolvedValue({
      code: 'B95B-RZ5Z', accountId: 7, lifetimeSeconds: 900,
      expiresAt: new Date(Date.now() + 900_000).toISOString(),
    })
    render(<AddNodeWizard join={join()} revealed={false} onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('mint-code'))
    expect(await screen.findByTestId('join-code')).toHaveTextContent('B95B-RZ5Z')
    expect(screen.getByTestId('code-command')).toHaveTextContent('/api/v2/j/B95B-RZ5Z')
  })

  it('counts down, because expiry is the one failure a token does not have', async () => {
    mint.mockResolvedValue({
      code: 'B95B-RZ5Z', accountId: 7, lifetimeSeconds: 900,
      expiresAt: new Date(Date.now() + 900_000).toISOString(),
    })
    render(<AddNodeWizard join={join()} revealed={false} onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('mint-code'))
    expect(await screen.findByTestId('code-expiry')).toHaveTextContent(/expires in 1[45]:/)
  })

  it('says expired rather than showing a dead code as usable', async () => {
    mint.mockResolvedValue({
      code: 'B95B-RZ5Z', accountId: 7, lifetimeSeconds: 900,
      expiresAt: new Date(Date.now() - 1000).toISOString(),
    })
    render(<AddNodeWizard join={join()} revealed={false} onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('mint-code'))
    expect(await screen.findByTestId('code-expiry')).toHaveTextContent('expired')
  })

  it('mints for the account that was chosen', async () => {
    mint.mockResolvedValue({
      code: 'X', accountId: 68, lifetimeSeconds: 900,
      expiresAt: new Date(Date.now() + 900_000).toISOString(),
    })
    render(<AddNodeWizard join={join()} revealed={false} onReveal={() => {}} />)
    await screen.findByTestId('node-account')
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByRole(
      'option', { name: /admin@bitport\.local/ }))
    fireEvent.click(screen.getByTestId('mint-code'))
    await screen.findByTestId('join-code')
    expect(mint).toHaveBeenCalledWith(68)
  })

  it('reports a failure instead of showing nothing', async () => {
    mint.mockRejectedValue(new Error('superadmin only'))
    render(<AddNodeWizard join={join()} revealed={false} onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('mint-code'))
    expect(await screen.findByTestId('code-error')).toHaveTextContent('superadmin only')
  })

  it('keeps the manual path available but out of the way', () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    expect(screen.getByTestId('node-addr').closest('.MuiCollapse-root'))
      .toHaveClass('MuiCollapse-hidden')
    fireEvent.click(screen.getByTestId('toggle-manual'))
    expect(screen.getByTestId('node-addr').closest('.MuiCollapse-root'))
      .not.toHaveClass('MuiCollapse-hidden')
  })
})

describe('naming the tenant', () => {
  it('offers domains, never a raw account id', async () => {
    /* "Account id" with a number in it earned exactly the question it
       deserved -- "what is account id?" -- for the second time, after the
       same mistake on the Services page. Nobody recognises 7. */
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    await screen.findByTestId('node-account')
    fireEvent.mouseDown(screen.getByRole('combobox'))
    const opts = (await screen.findAllByRole('option')).map((o) => o.textContent || '')
    expect(opts.some((t) => t.includes('source.acme.test'))).toBe(true)
    expect(opts.some((t) => t.trim() === '7')).toBe(false)
  })

  it('disambiguates tenants that share a domain pair', async () => {
    /* Live, three accounts point at the same pair. Three identical lines is
       the same problem as three numbers. */
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    await screen.findByTestId('node-account')
    fireEvent.mouseDown(screen.getByRole('combobox'))
    const opts = (await screen.findAllByRole('option')).map((o) => o.textContent || '')
    expect(opts.filter((t) => t.includes('(')).length).toBe(2)
    expect(opts.some((t) => t === 'only.test')).toBe(true)
  })

  it('does not ask a non-superadmin for every account', async () => {
    me.mockResolvedValue({ id: 7, email: 'ops@x.test', is_superadmin: false })
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    await screen.findByTestId('node-account')
    expect(admins).not.toHaveBeenCalled()
  })
})

describe('confirming a machine actually joined', () => {
  const liveCode = {
    code: 'B95B-RZ5Z', accountId: 7, lifetimeSeconds: 900,
    expiresAt: new Date(Date.now() + 900_000).toISOString(),
  }

  it('says it is waiting until one does', async () => {
    /* You ran a command on another computer and came back to a page showing
       zero nodes, with nothing saying whether it had worked. */
    mint.mockResolvedValue(liveCode)
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('mint-code'))
    expect(await screen.findByTestId('node-waiting')).toBeInTheDocument()
    expect(screen.queryByTestId('node-joined')).toBeNull()
  })

  it('confirms once the code is redeemed', async () => {
    mint.mockResolvedValue(liveCode)
    codeStatus.mockResolvedValue({ known: true, redeemed: true, expired: false,
                                   redeemedAt: '2026-09-12T00:00:00Z',
                                   redeemedFrom: '10.0.0.9' })
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    fireEvent.click(screen.getByTestId('mint-code'))
    expect(await screen.findByTestId('node-joined', {}, { timeout: 5000 }))
      .toBeInTheDocument()
  }, 10000)

  it('does not poll before there is a code to poll about', async () => {
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}} />)
    await screen.findByTestId('node-account')
    expect(codeStatus).not.toHaveBeenCalled()
  })

  it('reports the tenant upward so the other controls agree', async () => {
    const seen: number[] = []
    render(<AddNodeWizard join={join()} revealed onReveal={() => {}}
                          onAccountChange={(id) => seen.push(id)} />)
    await waitFor(() => expect(seen).toContain(7))
  })
})
