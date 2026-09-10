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
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import AddNodeWizard, { joinCommand } from './AddNodeWizard'
import type { NodeJoinDetails } from '@/api/controlPlane'

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
