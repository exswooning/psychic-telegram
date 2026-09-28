/**
 * History: every run this account has ever had, newest first -- migrate, delta,
 * seed, reset, wipe, full-setup, verify, tally, dms, trim-filler, repair. Nothing
 * here is a sample or a recent window; it is the account's whole record, exactly
 * as far back as it goes.
 *
 * Distinct from Jobs (what is running right now) and from any one run's own
 * report (what that run did in detail) -- this is the index across all of them,
 * the answer to "when did we last run X, and how did it go".
 */
import React, { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Alert, Box, Chip, IconButton, Paper, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, Tooltip, Typography,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import { fetchHistory, fetchMe, HistoryRun } from '@/api/controlPlane'
import { describeElapsed } from '@/hooks/useRunningJobs'

const when = (iso: string | null) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

const duration = (r: HistoryRun): string => {
  const start = r.startedAt ? Date.parse(r.startedAt) : NaN
  if (Number.isNaN(start)) return '—'
  const end = r.finishedAt ? Date.parse(r.finishedAt) : Date.now()
  return describeElapsed(Math.max(0, Math.round((end - start) / 1000)))
}

/** rc is a real exit code where one was observed, a negative number for a signal
 *  death -- judged `!= 0`, never `> 0` -- and null where none was ever observed
 *  (a restart mid-run, most often), which must read as unknown, never as clean. */
const Outcome: React.FC<{ r: HistoryRun }> = ({ r }) => {
  if (r.running) return <Chip size="small" color="info" label="running" />
  if (r.rc === null) return <Chip size="small" color="warning" label="unknown" />
  if (r.rc === 0) return <Chip size="small" color="success" label="finished" />
  if (r.rc < 0) return <Chip size="small" color="error" label={`crashed (signal ${-r.rc})`} />
  return <Chip size="small" color="error" label={`failed (exit ${r.rc})`} />
}

export const History: React.FC = () => {
  const [params] = useSearchParams()
  const asked = Number(params.get('account')) || undefined
  const [accountId, setAccountId] = useState<number | undefined>(asked)
  const [runs, setRuns] = useState<HistoryRun[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (accountId) return
    fetchMe().then((m) => setAccountId(m.id)).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])

  const load = useCallback(() => {
    if (!accountId) return
    fetchHistory(accountId).then((v) => { setRuns(v.runs); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 30_000); return () => clearInterval(t) }, [load])

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h5" sx={{ fontWeight: 700, flexGrow: 1 }}>History</Typography>
        <IconButton size="small" onClick={load} aria-label="refresh"><RefreshIcon fontSize="small" /></IconButton>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 780 }}>
        Every run this account has ever had, whatever launched it -- a migration, a
        seed, a reset, a repair, a verify or tally pass. Nothing is summarised away.
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <Paper variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Job</TableCell><TableCell>Started</TableCell>
              <TableCell>Finished</TableCell><TableCell>Duration</TableCell>
              <TableCell>Outcome</TableCell><TableCell>Detail</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {(runs ?? []).map((r, i) => (
              <TableRow key={`${r.jobName}-${r.pid}-${r.startedAt}-${i}`} hover
                        data-testid={`history-row-${i}`}>
                <TableCell><Chip size="small" variant="outlined" label={r.jobName} /></TableCell>
                <TableCell><Typography variant="body2">{when(r.startedAt)}</Typography></TableCell>
                <TableCell><Typography variant="body2">{when(r.finishedAt)}</Typography></TableCell>
                <TableCell><Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  {duration(r)}
                </Typography></TableCell>
                <TableCell><Outcome r={r} /></TableCell>
                <TableCell sx={{ maxWidth: 360 }}>
                  {r.detail ? (
                    <Tooltip title={r.detail}>
                      <Typography variant="body2" color="text.secondary" noWrap>{r.detail}</Typography>
                    </Tooltip>
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
            {(runs ?? []).length === 0 && (
              <TableRow><TableCell colSpan={6}>
                <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                  {runs ? 'No runs recorded for this account yet.' : 'Loading…'}
                </Typography>
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Paper>
    </Box>
  )
}

export default History
