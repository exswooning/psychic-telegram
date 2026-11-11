import React, { useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { Alert, Box, Chip, Link, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography } from '@mui/material'
import { fetchOneToOne } from '@/api/controlPlane'
import type { OneToOneUser, OneToOneView } from '@/api/controlPlane'
import { serviceSummary, VERDICT } from '@/oneToOneVerdicts'

const resultLabel = (user: OneToOneUser) => {
  const hasExtras = user.services.some((service) => (service.counts?.extras ?? 0) > 0)
  if (user.verdict === 'IDENTICAL' && hasExtras) {
    return { label: 'Needs review', color: 'warning' as const }
  }
  return VERDICT[user.verdict]
}

/** Results are saved per service, not tied to the current job's completion. */
const VerificationUserResults: React.FC<{ accountId?: number }> = ({ accountId }) => {
  const [view, setView] = useState<OneToOneView | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    setView(null)
    setError('')
    if (!accountId) return
    let active = true
    const load = () => {
      fetchOneToOne(accountId).then((next) => {
        if (active) { setView(next); setError('') }
      }).catch(() => { if (active) setError('Could not refresh user results. Displayed results may be older.') })
    }
    load()
    const timer = window.setInterval(load, 15000)
    return () => { active = false; window.clearInterval(timer) }
  }, [accountId])

  const users = (view?.users ?? []).filter((u) => u.services.some((s) => Boolean(s.verifiedAt)))
    .sort((a, b) => (b.verifiedAt ?? '').localeCompare(a.verifiedAt ?? '') || a.user.localeCompare(b.user))
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="verification-user-results">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <Typography variant="h6" sx={{ fontWeight: 600, flexGrow: 1 }}>User verification results</Typography>
        {accountId && <Link component={RouterLink} to={`/one-to-one?account=${accountId}`}>View details</Link>}
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {view ? `${users.length} of ${view.users.length} users have saved results. ` : ''}
        Services are saved as their checks finish. Results may be from earlier runs; they do not prove the current run or full parity is complete.
      </Typography>
      {error && <Alert severity="warning" sx={{ mb: 1 }}>{error}</Alert>}
      {!view && !error && <Typography variant="body2">Loading results…</Typography>}
      {view && !users.length && <Typography variant="body2" color="text.secondary">No user checks have been saved yet.</Typography>}
      {users.length > 0 && <TableContainer sx={{ maxHeight: 420 }}>
        <Table size="small" stickyHeader aria-label="Saved user verification results">
          <TableHead><TableRow><TableCell>User</TableCell><TableCell>Saved verdict</TableCell>
            <TableCell>Services with results</TableCell><TableCell>Last saved</TableCell></TableRow></TableHead>
          <TableBody>{users.map((u) => {
            const verdict = resultLabel(u)
            const recorded = u.services.filter((s) => Boolean(s.verifiedAt))
            const extras = recorded.some((s) => (s.counts?.extras ?? 0) > 0)
            const sample = recorded.some((s) => s.sampledOf != null && s.sampledOf > s.checked)
            return <TableRow key={u.user} data-testid={`saved-result-${u.user}`}>
              <TableCell>{u.user}</TableCell>
              <TableCell><Chip size="small" color={verdict.color} label={verdict.label} />
                {extras && <Typography variant="caption" display="block">Target-only items need review</Typography>}
                {sample && <Typography variant="caption" display="block">Includes sampled checks</Typography>}
              </TableCell>
              <TableCell><Typography variant="caption">{recorded.map(serviceSummary).join('; ')}</Typography></TableCell>
              <TableCell><Typography variant="caption">{u.verifiedAt ? new Date(u.verifiedAt).toLocaleString() : 'Unknown'}</Typography></TableCell>
            </TableRow>
          })}</TableBody>
        </Table>
      </TableContainer>}
      {view && <Box sx={{ mt: 1 }}><Typography variant="caption" color="text.secondary">
        Users without a saved service result are not listed. Open details to see all users and findings.
      </Typography></Box>}
    </Paper>
  )
}

export default VerificationUserResults
