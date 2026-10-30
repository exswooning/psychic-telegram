import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MailLinkScans } from './MailLinkScans'
import type { MailLinkScanReport } from '../api/controlPlane'

const base: MailLinkScanReport = {
  source: 'info@s.example.com', mode: 'search', state: 'DONE', startedAt: '2026-10-30T10:00:00Z',
  messages: 13846, candidates: 412, examined: 412, blocked: 0, notRead: 13434, bodiesRead: 412,
  bytesRead: 3 << 20, attachmentReads: 2, listCalls: 16, knownLinkMessages: 87, recallMissed: 0, scanSeconds: 95,
}

describe('MailLinkScans', () => {
  it('says what was read and what was left unread', () => {
    render(<MailLinkScans scans={[base]} />)
    const a = screen.getByTestId('mail-link-scans')
    expect(a).toHaveTextContent('412 of 412 candidates read')
    expect(a).toHaveTextContent('13,434 not read')
    expect(a).toHaveTextContent('search missed 0 of 87')
    expect(a).toHaveTextContent('goes to the DMS unrewritten')
  })

  it('warns when the search missed known link mail or a scan was blocked', () => {
    render(<MailLinkScans scans={[{ ...base, recallMissed: 3 }]} />)
    expect(screen.getByTestId('mail-link-scans').className).toMatch(/Warning/)
  })

  it('shows nothing without a scan', () => {
    const { container } = render(<MailLinkScans />)
    expect(container).toBeEmptyDOMElement()
  })
})
