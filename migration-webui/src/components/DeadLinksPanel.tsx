/**
 * The list behind the link scan's "N links to source files that answer 404": every Drive link in the
 * account's mail with no copy on the target, what the source answers for that file, and how many emails
 * mention it. Written by mail_dead_links.py; shown only once a check has run.
 */
import React, { useEffect, useState } from 'react'
import { Alert, Box, Chip, Link, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography } from '@mui/material'
import { deadLinksCsvUrl, fetchDeadLinks } from '@/api/controlPlane'
import type { DeadLinkStatus, DeadLinksView } from '@/api/controlPlane'

const KINDS: DeadLinkStatus[] = ['NOT_FOUND', 'NO_ACCESS', 'OUTSIDE', 'NOT_MIGRATED', 'OTHER']

export const DeadLinksPanel: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [status, setStatus] = useState<DeadLinkStatus | null>(null)
  const [q, setQ] = useState('')
  const [view, setView] = useState<DeadLinksView | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const t = setTimeout(() => {
      fetchDeadLinks(accountId, status, q.trim()).then((v) => { setView(v); setError('') })
        .catch((e) => setError(e instanceof Error ? e.message : String(e)))
    }, 250)
    return () => clearTimeout(t)
  }, [accountId, status, q])

  if (error) return <Alert severity="warning" sx={{ mb: 2 }} data-testid="dead-links-error">Mail links: {error}</Alert>
  const totals = view?.totals as Record<string, { label: string; links: number; emails: number }> | undefined
  if (!view || !totals || !Object.keys(totals).length) return null
  if (!KINDS.some((k) => totals[k]?.links)) return null

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="dead-links-panel">
      <Stack direction="row" alignItems="baseline" justifyContent="space-between" flexWrap="wrap" gap={1}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Mail links that could not be repointed</Typography>
        <Link href={deadLinksCsvUrl(accountId, status, q.trim())} data-testid="dead-links-csv" variant="body2">Download CSV</Link>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        These links name a Drive file with no copy on the target, so they stay exactly as they were in the email.
        Choose a kind to list it.
      </Typography>
      <Stack direction="row" gap={1} flexWrap="wrap" sx={{ mb: 1.5 }} role="group" aria-label="What the source answers">
        {KINDS.filter((k) => totals[k]?.links).map((k) => (
          <Chip key={k} clickable variant={status === k ? 'filled' : 'outlined'} aria-pressed={status === k}
                color={k === 'NOT_FOUND' ? 'default' : 'info'} data-testid={`dead-kind-${k}`}
                onClick={() => setStatus(status === k ? null : k)}
                label={`${totals[k].label}: ${totals[k].links.toLocaleString()} files · ${totals[k].emails.toLocaleString()} emails`} />
        ))}
      </Stack>
      <TextField id="dead-links-search" size="small" label="Search a user, file id or owner" value={q}
                 onChange={(e) => setQ(e.target.value)} sx={{ mb: 1, width: { xs: '100%', sm: 380 } }} />
      <Box sx={{ overflowX: 'auto' }}>
        <Table size="small" data-testid="dead-links-table">
          <TableHead><TableRow>
            <TableCell>Source answers</TableCell><TableCell>Emails</TableCell><TableCell>User</TableCell><TableCell>File</TableCell>
          </TableRow></TableHead>
          <TableBody>
            {view.links.map((l) => (
              <TableRow key={`${l.user}:${l.fileId}`}>
                <TableCell>{totals[l.status]?.label ?? l.status}{l.detail ? ` (${l.detail})` : ''}</TableCell>
                <TableCell sx={{ fontVariantNumeric: 'tabular-nums' }}>{l.emails.toLocaleString()}</TableCell>
                <TableCell>{l.user.split('@')[0]}</TableCell>
                <TableCell sx={{ wordBreak: 'break-all' }}>
                  <Link href={l.sourceUrl} target="_blank" rel="noreferrer">{l.fileId}</Link>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
        Showing {view.links.length.toLocaleString()} of {view.matched.toLocaleString()}
      </Typography>
    </Paper>
  )
}

export default DeadLinksPanel
