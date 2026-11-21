/**
 * Every sharing grant the migration made, tenant-wide: inside the organisation, outside it, or
 * open to anyone with the link. From the account's ledger (`/api/v2/sharing`), so opening it costs
 * no Google call. A grant's role is recorded from 9 Nov 2026 on; older grants show "not recorded".
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, Link, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material'
import { fetchSharing, sharingCsvUrl } from '@/api/controlPlane'
import type { SharingKind, SharingView } from '@/api/controlPlane'

const KINDS: SharingKind[] = ['external', 'public', 'internal', 'domain']
const PAGE = 50
const tone = (k: SharingKind) => (k === 'public' ? 'warning' : k === 'external' ? 'info' : 'default') as
  'warning' | 'info' | 'default'

export const SharingPanel: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [kind, setKind] = useState<SharingKind | null>(null)
  const [q, setQ] = useState('')
  const [shown, setShown] = useState(PAGE)
  const [view, setView] = useState<SharingView | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    fetchSharing(accountId, kind, q.trim(), 0, shown)
      .then((v) => { setView(v); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId, kind, q, shown])
  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t) }, [load])

  if (error) return <Alert severity="warning" sx={{ mb: 2 }} data-testid="sharing-error">Sharing: {error}</Alert>
  if (!view) return null
  const total = KINDS.reduce((n, k) => n + view.totals[k].grants, 0)
  if (!total) return null
  const pick = (k: SharingKind) => { setKind(kind === k ? null : k); setShown(PAGE) }

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="sharing-panel">
      <Stack direction="row" alignItems="baseline" justifyContent="space-between" flexWrap="wrap" gap={1}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
          Sharing — who this migration gave access ({total.toLocaleString()} grants)
        </Typography>
        <Link href={sharingCsvUrl(accountId, kind, q.trim())} data-testid="sharing-csv" variant="body2">
          Download CSV{kind ? ` (${view.totals[kind].label.toLowerCase()})` : ''}
        </Link>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        The same grants the source had, recreated on the target copies. Choose a kind to list it.
      </Typography>

      <Stack direction="row" gap={1} flexWrap="wrap" sx={{ mb: 1.5 }} role="group" aria-label="Kind of grant">
        {KINDS.map((k) => (
          <Chip key={k} clickable color={tone(k)} variant={kind === k ? 'filled' : 'outlined'}
                aria-pressed={kind === k} onClick={() => pick(k)} data-testid={`sharing-kind-${k}`}
                label={`${view.totals[k].label}: ${view.totals[k].grants.toLocaleString()} grants · ${view.totals[k].files.toLocaleString()} files`} />
        ))}
      </Stack>

      <Typography variant="body2" sx={{ mb: 1 }} data-testid="sharing-roles">
        Roles: {Object.entries(view.roles).sort((a, b) => b[1] - a[1])
          .map(([r, n]) => `${r} ${n.toLocaleString()}`).join(' · ')}
        {Object.keys(view.notGranted).length > 0 && (
          <> — not granted: {Object.entries(view.notGranted).map(([s, n]) => `${s} ${n}`).join(', ')}</>
        )}
      </Typography>

      {(view.externalPeople.length > 0 || view.externalDomains.length > 0) && (
        <Stack direction={{ xs: 'column', md: 'row' }} gap={3} sx={{ mb: 1.5 }}>
          {[['Outside people with the most files', view.externalPeople],
            ['Outside domains', view.externalDomains]].map(([title, list]) => (
            <Box key={title as string} sx={{ flex: 1, minWidth: 0 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{title as string}</Typography>
              {(list as { name: string; files: number }[]).slice(0, 8).map((p) => (
                <Typography key={p.name} variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  <Link component="button" onClick={() => { setQ(p.name); setShown(PAGE) }}>{p.name}</Link>
                  {' '}— {p.files.toLocaleString()} file{p.files === 1 ? '' : 's'}
                </Typography>
              ))}
            </Box>
          ))}
        </Stack>
      )}

      <TextField id="sharing-search" size="small" label="Search a person, domain, file or owner" value={q}
                 onChange={(e) => { setQ(e.target.value); setShown(PAGE) }} sx={{ mb: 1, width: { xs: '100%', sm: 380 } }} />
      <Box sx={{ overflowX: 'auto' }}>
        <Table size="small" data-testid="sharing-table">
          <TableHead>
            <TableRow>
              <TableCell>Kind</TableCell><TableCell>Who</TableCell><TableCell>Role</TableCell>
              <TableCell>File (target copy)</TableCell><TableCell>Owner</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {view.grants.map((g) => (
              <TableRow key={`${g.sourceId}:${g.grantee}`}>
                <TableCell><Chip size="small" color={tone(g.kind)} variant="outlined" label={view.totals[g.kind].label} /></TableCell>
                <TableCell sx={{ wordBreak: 'break-all' }}>{g.grantee}</TableCell>
                <TableCell>{g.role ?? <Typography component="span" variant="body2" color="text.secondary">not recorded</Typography>}</TableCell>
                <TableCell sx={{ wordBreak: 'break-word' }}>
                  {g.targetUrl ? <Link href={g.targetUrl} target="_blank" rel="noreferrer">{g.file}</Link> : g.file}
                  {' '}<Link href={g.sourceUrl} target="_blank" rel="noreferrer" variant="caption">(source)</Link>
                </TableCell>
                <TableCell>{g.owner.split('@')[0]}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Box>
      <Stack direction="row" alignItems="center" gap={2} sx={{ mt: 1 }}>
        <Typography variant="body2" color="text.secondary">
          Showing {view.grants.length.toLocaleString()} of {view.matched.toLocaleString()}
        </Typography>
        {view.grants.length < view.matched && (
          <Button size="small" onClick={() => setShown(shown + PAGE)} data-testid="sharing-more">Show more</Button>
        )}
      </Stack>
    </Paper>
  )
}

export default SharingPanel
