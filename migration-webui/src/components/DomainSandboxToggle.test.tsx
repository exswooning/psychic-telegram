/**
 * The only way to seed or reset a protected domain used to be SSH and
 * `domain_guard.py --revoke`. This is that control, reached without a
 * terminal -- a switch, typed-confirm gated to turn ON, unguarded to
 * turn OFF (the safe direction).
 */
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom'

vi.mock('@/api/controlPlane', () => ({
  fetchDomainGuardStatus: vi.fn(),
  revokeDomainGuard: vi.fn(),
  restoreDomainGuard: vi.fn(),
}))
import {
  fetchDomainGuardStatus, revokeDomainGuard, restoreDomainGuard,
} from '@/api/controlPlane'
import DomainSandboxToggle from './DomainSandboxToggle'

const status = fetchDomainGuardStatus as unknown as ReturnType<typeof vi.fn>
const revoke = revokeDomainGuard as unknown as ReturnType<typeof vi.fn>
const restore = restoreDomainGuard as unknown as ReturnType<typeof vi.fn>

beforeEach(() => { status.mockReset(); revoke.mockReset(); restore.mockReset() })

describe('a protected domain', () => {
  it('renders nothing until the status read resolves', async () => {
    status.mockReturnValue(new Promise(() => {}))
    render(<DomainSandboxToggle domain="client.example" />)
    expect(screen.queryByTestId('sandbox-toggle')).toBeNull()
  })

  it('shows the switch off, and says protection is in effect', async () => {
    status.mockResolvedValue({ domain: 'client.example', protected: true })
    render(<DomainSandboxToggle domain="client.example" />)
    expect(await screen.findByTestId('sandbox-toggle')).not.toBeChecked()
    expect(screen.getByText(/protected — seeding and reset are refused/))
      .toBeInTheDocument()
  })

  it('opens the declare-sandbox dialog rather than acting immediately', async () => {
    status.mockResolvedValue({ domain: 'client.example', protected: true })
    render(<DomainSandboxToggle domain="client.example" />)
    fireEvent.click(await screen.findByTestId('sandbox-toggle'))
    expect(await screen.findByText(/Declare client\.example a sandbox\?/))
      .toBeInTheDocument()
    expect(revoke).not.toHaveBeenCalled()
  })

  it('will not declare it without the domain typed back', async () => {
    status.mockResolvedValue({ domain: 'client.example', protected: true })
    render(<DomainSandboxToggle domain="client.example" />)
    fireEvent.click(await screen.findByTestId('sandbox-toggle'))
    await screen.findByTestId('sandbox-confirm')
    fireEvent.change(screen.getByTestId('sandbox-reason'),
                     { target: { value: 'rehearsal only' } })
    expect(screen.getByTestId('sandbox-confirm')).toBeDisabled()
    fireEvent.change(screen.getByTestId('sandbox-confirm-domain'),
                     { target: { value: 'wrong.example' } })
    expect(screen.getByTestId('sandbox-confirm')).toBeDisabled()
  })

  it('will not declare it without a reason', async () => {
    status.mockResolvedValue({ domain: 'client.example', protected: true })
    render(<DomainSandboxToggle domain="client.example" />)
    fireEvent.click(await screen.findByTestId('sandbox-toggle'))
    await screen.findByTestId('sandbox-confirm')
    fireEvent.change(screen.getByTestId('sandbox-confirm-domain'),
                     { target: { value: 'client.example' } })
    expect(screen.getByTestId('sandbox-confirm')).toBeDisabled()
  })

  it('declares it once both the domain and a reason are given', async () => {
    status.mockResolvedValue({ domain: 'client.example', protected: true })
    revoke.mockResolvedValue({ ok: true, detail: 'protection off' })
    render(<DomainSandboxToggle domain="client.example" />)
    fireEvent.click(await screen.findByTestId('sandbox-toggle'))
    await screen.findByTestId('sandbox-confirm')
    fireEvent.change(screen.getByTestId('sandbox-confirm-domain'),
                     { target: { value: 'client.example' } })
    fireEvent.change(screen.getByTestId('sandbox-reason'),
                     { target: { value: 'rehearsal tenant' } })
    fireEvent.click(screen.getByTestId('sandbox-confirm'))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith(
      'client.example', 'rehearsal tenant'))
  })

  it('surfaces a refusal instead of silently closing', async () => {
    status.mockResolvedValue({ domain: 'client.example', protected: true })
    revoke.mockResolvedValue({ ok: false, detail: 'not a superadmin' })
    render(<DomainSandboxToggle domain="client.example" />)
    fireEvent.click(await screen.findByTestId('sandbox-toggle'))
    await screen.findByTestId('sandbox-confirm')
    fireEvent.change(screen.getByTestId('sandbox-confirm-domain'),
                     { target: { value: 'client.example' } })
    fireEvent.change(screen.getByTestId('sandbox-reason'),
                     { target: { value: 'rehearsal tenant' } })
    fireEvent.click(screen.getByTestId('sandbox-confirm'))
    expect(await screen.findByTestId('sandbox-error'))
      .toHaveTextContent('not a superadmin')
  })
})

describe('a declared sandbox', () => {
  it('shows the switch on, who declared it and why', async () => {
    status.mockResolvedValue({
      domain: 'sandbox.example', protected: false,
      revokedBy: 'aryan.admin@bitport.local', revokedReason: 'rehearsal only',
    })
    render(<DomainSandboxToggle domain="sandbox.example" />)
    expect(await screen.findByTestId('sandbox-toggle')).toBeChecked()
    expect(screen.getByText(/a declared sandbox — protection is off/))
      .toBeInTheDocument()
    expect(screen.getByTestId('sandbox-revoked-by')).toHaveTextContent(
      'declared by aryan.admin@bitport.local — rehearsal only')
  })

  it('restores protection with one click, no dialog', async () => {
    status.mockResolvedValue({
      domain: 'sandbox.example', protected: false,
      revokedBy: 'a', revokedReason: 'r',
    })
    restore.mockResolvedValue({ ok: true, detail: 'protection restored' })
    render(<DomainSandboxToggle domain="sandbox.example" />)
    fireEvent.click(await screen.findByTestId('sandbox-toggle'))
    await waitFor(() => expect(restore).toHaveBeenCalledWith(
      'sandbox.example', expect.any(String)))
    expect(screen.queryByText(/Declare .* a sandbox\?/)).toBeNull()
  })
})
