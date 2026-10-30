import React from 'react'
import { Alert, Typography } from '@mui/material'
import type { MailLinkScanReport } from '../api/controlPlane'

const kb = (n: number) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`)

function line(r: MailLinkScanReport): string {
  const parts = [r.mode === 'search' ? 'search-first' : 'exhaustive', r.state.toLowerCase()]
  if (typeof r.messages === 'number') parts.push(`${r.messages.toLocaleString()} messages`)
  if (r.mode === 'search' && typeof r.candidates === 'number') {
    parts.push(`${r.examined.toLocaleString()} of ${r.candidates.toLocaleString()} candidates read`)
    parts.push(`${r.notRead.toLocaleString()} not read`)
  } else {
    parts.push(`${r.examined.toLocaleString()} read`)
  }
  parts.push(`${kb(r.bytesRead)} of bodies`)
  if (r.blocked) parts.push(`${r.blocked.toLocaleString()} blocked`)
  if (typeof r.recallMissed === 'number') {
    parts.push(`search missed ${r.recallMissed} of ${r.knownLinkMessages ?? 0} messages already known to carry a link`)
  }
  if (typeof r.scanSeconds === 'number') parts.push(`${r.scanSeconds}s`)
  return parts.join(' · ')
}

/** Each user's split-mail link scan: what Gmail search named, what was read, what was not. */
export const MailLinkScans: React.FC<{ scans?: MailLinkScanReport[] }> = ({ scans }) => {
  if (!scans?.length) return null
  const attention = scans.some((r) => r.blocked || r.recallMissed || r.state === 'INTERRUPTED')
  const search = scans.some((r) => r.mode === 'search')
  return (
    <Alert severity={attention ? 'warning' : 'info'} sx={{ mb: 2 }} data-testid="mail-link-scans">
      Mail link scan (before any mail is copied).
      {scans.map((r) => <Typography variant="body2" key={r.source}>{r.source}: {line(r)}</Typography>)}
      {search && <Typography variant="caption" sx={{ display: 'block', mt: 0.5 }}>
        Search-first reads only the messages Gmail&apos;s search names as carrying a Drive link. A link
        Gmail does not index goes to the DMS unrewritten; an Exhaustive link scan reads every message.
      </Typography>}
    </Alert>
  )
}
