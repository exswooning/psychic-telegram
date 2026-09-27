/**
 * The one-to-one check, as a section on the migration itself -- not the whole
 * One-to-one page, just its totals and whoever needs attention, so a job page
 * does not have to be left to see whether the users it just finished came back
 * clean. Same source (`/api/v2/one-to-one`) and the same rule: a user nobody
 * has checked is NOT VERIFIED, never a blank, and INCOMPLETE is amber, never
 * green.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import {
  Alert, Box, Chip, Link, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material'
import { fetchOneToOne } from '@/api/controlPlane'
import type { OneToOneView } from '@/api/controlPlane'
import { ORDER, VERDICT } from '@/oneToOneVerdicts'

const when = (iso: string | null) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

const MAX_ROWS = 8

export const OneToOneSummary: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [view, setView] = useState<OneToOneView | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    fetchOneToOne(accountId).then((v) => { setView(v); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])

  if (error) return <Alert severity="warning" sx={{ mb: 2 }} data-testid="one-to-one-summary-error">{error}</Alert>
  if (!view) return null

  const totals = view.totals ?? {}
  // Nothing mapped yet, and nothing checked either: the section would be all zeroes and a
  // link to an equally empty page, so it stays out of the way until there is something to say.
  const nothingYet = ORDER.every((k) => !(totals[k] ?? 0))
  if (nothingYet) return null

  const attention = [...view.users].filter((u) => u.verdict === 'DIFFERENCES' || u.verdict === 'INCOMPLETE')
    .sort((a, b) => (a.verdict === b.verdict ? a.user.localeCompare(b.user) : a.verdict === 'DIFFERENCES' ? -1 : 1))

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="one-to-one-summary">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <Typography variant="h6" sx={{ fontWeight: 700, flexGrow: 1 }}>One-to-one check</Typography>
        <Link component={RouterLink} to={`/one-to-one?account=${accountId}`} variant="body2">
          View all &rarr;
        </Link>
      </Stack>
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: attention.length ? 1.5 : 0 }}>
        {ORDER.map((k) => (
          <Chip key={k} size="small" color={VERDICT[k].color} variant={(totals[k] ?? 0) ? 'filled' : 'outlined'}
                label={`${totals[k] ?? 0} ${VERDICT[k].label.toLowerCase()}`} data-testid={`o2o-summary-total-${k}`} />
        ))}
      </Stack>
      {attention.length > 0 && (
        <Table size="small">
          <TableHead>
            <TableRow><TableCell>User</TableCell><TableCell>Verdict</TableCell><TableCell>Last checked</TableCell></TableRow>
          </TableHead>
          <TableBody>
            {attention.slice(0, MAX_ROWS).map((u) => (
              <TableRow key={u.user}>
                <TableCell>{u.user}</TableCell>
                <TableCell><Chip size="small" color={VERDICT[u.verdict].color} label={VERDICT[u.verdict].label} /></TableCell>
                <TableCell><Typography variant="caption">{when(u.verifiedAt)}</Typography></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {attention.length > MAX_ROWS && (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          and {attention.length - MAX_ROWS} more -- see the full list.
        </Typography>
      )}
    </Paper>
  )
}

export default OneToOneSummary
