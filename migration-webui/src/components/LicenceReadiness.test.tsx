import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

const fetchLicencePreflight = vi.hoisted(() => vi.fn())
vi.mock('@/api/client', () => ({ fetchLicencePreflight }))

import LicenceReadiness from './LicenceReadiness'

const pf = (o = {}) => ({
  pairs: 200, sourceLicensed: 200, targetLicensed: 200, shortfall: 0,
  unlicensedTargets: [], mergedTargets: [],
  sourceDomain: 'src.test', targetDomain: 'tgt.test',
  sourceError: '', targetError: '', ...o,
})

describe('licence readiness, before a run', () => {
  it('warns when a target cannot receive anything', async () => {
    fetchLicencePreflight.mockResolvedValue(pf({
      targetLicensed: 150, shortfall: 50,
      unlicensedTargets: ['a@tgt.test', 'b@tgt.test'],
    }))
    render(<LicenceReadiness />)
    await screen.findByText(/50 of 200 users/)
    // The remedies the operator actually has, not just the bad news.
    expect(screen.getByText(/Migrate fewer users/)).toBeInTheDocument()
    expect(screen.getByText(/Merge several source users/)).toBeInTheDocument()
  })

  it('names the errors Google actually returns', async () => {
    /* None of them contain the word "licence", which is why this panel
       exists at all. */
    fetchLicencePreflight.mockResolvedValue(pf({ shortfall: 1 }))
    render(<LicenceReadiness />)
    await screen.findByText(/Mail service not enabled/)
    expect(screen.getByText(/Active session is invalid/)).toBeInTheDocument()
    expect(screen.getByText(/enough available licenses/)).toBeInTheDocument()
  })

  it('says nothing alarming when every target is licensed', async () => {
    fetchLicencePreflight.mockResolvedValue(pf())
    render(<LicenceReadiness />)
    await screen.findByText('every target is licensed')
    expect(screen.queryByText(/Two ways forward/)).not.toBeInTheDocument()
  })

  it('admits it could not read licences rather than showing a zero', async () => {
    /* The scope most tenants have never granted. A confident 0 shortfall
       built from an empty list is the one answer this must never give. */
    fetchLicencePreflight.mockResolvedValue(pf({
      targetLicensed: 0, targetError: 'licence data needs the apps.licensing scope',
    }))
    render(<LicenceReadiness />)
    await screen.findByText(/Licences could not be read/)
    expect(screen.queryByText('every target is licensed')).not.toBeInTheDocument()
    expect(screen.queryByText('Would migrate nothing')).not.toBeInTheDocument()
  })

  it('lists the unlicensed targets on request', async () => {
    fetchLicencePreflight.mockResolvedValue(pf({
      shortfall: 2, unlicensedTargets: ['a@tgt.test', 'b@tgt.test'],
    }))
    render(<LicenceReadiness />)
    fireEvent.click(await screen.findByText(/Show the 2 unlicensed targets/))
    await waitFor(() => expect(screen.getByText('a@tgt.test')).toBeVisible())
  })

  it('reports a merge the ledger is already doing', async () => {
    fetchLicencePreflight.mockResolvedValue(pf({
      mergedTargets: [{ target: 'one@tgt.test', sources: 3 }],
    }))
    render(<LicenceReadiness />)
    await screen.findByText(/one@tgt.test \(3\)/)
  })
})
