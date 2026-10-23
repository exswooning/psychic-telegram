/**
 * Every kind of one-to-one finding this account has had: why it happens, how it is
 * fixed, whether that fix runs on its own the next time, and how many are open and
 * have been fixed (known_issues.py, issue_fix_log). Only kinds ever seen are shown.
 */
import React, { useEffect, useState } from 'react'
import { Box, Chip, Collapse, IconButton, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ExpandLessIcon from '@mui/icons-material/ExpandLess'
import { fetchKnownIssues } from '@/api/controlPlane'
import type { KnownIssueKind, KnownIssues } from '@/api/controlPlane'

const when = (s: string | null) => (s ? new Date(s).toLocaleString() : '—')

const Row: React.FC<{ k: KnownIssueKind }> = ({ k }) => {
  const [open, setOpen] = useState(false)
  return (
    <>
      <TableRow hover data-testid={`issue-${k.id}`}>
        <TableCell padding="checkbox">
          <IconButton size="small" aria-label={open ? `hide how ${k.title} is fixed` : `show how ${k.title} is fixed`}
                      onClick={() => setOpen(!open)}>
            {open ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
          </IconButton>
        </TableCell>
        <TableCell>
          <Typography variant="body2">{k.title}</Typography>
          <Typography variant="caption" color="text.secondary">{k.service}</Typography>
        </TableCell>
        <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>{k.open}</TableCell>
        <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>{k.fixed}</TableCell>
        <TableCell>{when(k.lastFixed)}</TableCell>
        <TableCell>
          <Chip size="small" color={k.auto ? 'success' : 'warning'} variant="outlined"
                label={k.auto ? 'Fixed automatically' : 'Needs a person'} />
        </TableCell>
      </TableRow>
      <TableRow>
        <TableCell colSpan={6} sx={{ py: 0, borderBottom: open ? undefined : 'none' }}>
          <Collapse in={open} unmountOnExit>
            <Box sx={{ py: 1, maxWidth: 820 }}>
              <Typography variant="body2"><strong>Why:</strong> {k.cause || '—'}</Typography>
              <Typography variant="body2" sx={{ mt: 0.5 }}><strong>How it is fixed:</strong> {k.fix}</Typography>
            </Box>
          </Collapse>
        </TableCell>
      </TableRow>
    </>
  )
}

const KnownIssuesPanel: React.FC<{ accountId?: number }> = ({ accountId }) => {
  const [data, setData] = useState<KnownIssues | null>(null)
  useEffect(() => {
    if (!accountId) return
    const load = () => fetchKnownIssues(accountId).then(setData).catch(() => setData(null))
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [accountId])
  const seen = (data?.kinds ?? []).filter((k) => k.open > 0 || k.fixed > 0)
  if (!data || !seen.length) return null
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="known-issues">
      <Stack direction="row" alignItems="baseline" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>Known issues and how they are fixed</Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, maxWidth: 820 }}>
        Each kind of difference the check has found on this account. A kind fixed automatically is
        repaired on the target the next time it is found; a fix is counted only when a fresh, complete
        check no longer finds it.
      </Typography>
      <Box sx={{ overflowX: 'auto' }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell />
              <TableCell>Kind</TableCell>
              <TableCell align="right">Open</TableCell>
              <TableCell align="right">Fixed</TableCell>
              <TableCell>Last fixed</TableCell>
              <TableCell>Next time</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>{seen.map((k) => <Row key={k.id} k={k} />)}</TableBody>
        </Table>
      </Box>
    </Paper>
  )
}

export default KnownIssuesPanel
