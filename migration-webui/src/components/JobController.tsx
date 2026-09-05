import React, { useMemo, useState } from 'react'
import {
  Box, Button, Checkbox, Chip, LinearProgress, Paper, Stack, Table,
  TableBody, TableCell, TableContainer, TableHead, TableRow, ToggleButton,
  ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material'
import {
  PlayArrow as StartIcon, Stop as StopIcon, Replay as RetryIcon,
  Science as DryRunIcon,
} from '@mui/icons-material'
import {
  UserProgress, FleetNode, startMigration, stopJob,
} from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * Per-user and per-batch job control.
 *
 * The design driver is the **partial failure** state the operator actually
 * faces: on this tenant pair a batch is routinely 7 DONE / 1 RUNNING /
 * 2 FAILED at the same moment. Two consequences:
 *
 *  1. **No single batch percentage.** Averaging 9 users into one number hides
 *     which ones need attention, and averaging *across* a permanently-dead
 *     account makes the batch look broken forever. Status counts are shown
 *     as separate chips and progress stays per-row.
 *  2. **Selection drives scope.** Acting on "the batch" when two users are
 *     unfixable is how an operator ends up re-running 12,000 healthy files to
 *     chase 8 broken ones. Select the rows, act on those.
 */
type StatusFilter = 'all' | 'RUNNING' | 'FAILED' | 'DONE' | 'PENDING'

interface Props {
  users: UserProgress[]
  nodes: FleetNode[]
  onChanged?: () => void
}

const STATUS_COLOR: Record<string, 'success' | 'primary' | 'error' | 'default'> = {
  DONE: 'success', RUNNING: 'primary', FAILED: 'error', PENDING: 'default',
}

const JobController: React.FC<Props> = ({ users, nodes, onChanged }) => {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [pending, setPending] = useState<null | {
    title: string; description: React.ReactNode; run: (reason: string) => Promise<void>
  }>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const counts = useMemo(() => {
    const c: Record<string, number> = { DONE: 0, RUNNING: 0, FAILED: 0, PENDING: 0 }
    users.forEach((u) => { c[u.status] = (c[u.status] ?? 0) + 1 })
    return c
  }, [users])

  const shown = useMemo(
    () => (filter === 'all' ? users : users.filter((u) => u.status === filter)),
    [users, filter],
  )

  const runningJob = nodes.find((n) => n.active_job && n.job_pid)

  const toggle = (email: string) => setSelected((prev) => {
    const next = new Set(prev)
    next.has(email) ? next.delete(email) : next.add(email)
    return next
  })

  const submit = async (reason: string) => {
    if (!pending) return
    setBusy(true); setError(null)
    try {
      await pending.run(reason)
      setPending(null); setSelected(new Set()); onChanged?.()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const askStart = (dryRun: boolean) => {
    const targets = Array.from(selected)
    setPending({
      title: dryRun ? 'Start dry run' : 'Start migration',
      description: (
        <>
          {dryRun
            ? 'Logs every intended write and performs none. '
            : 'Copies real data into the target tenant. '}
          Scope: <strong>{targets.length ? `${targets.length} selected user(s)` : 'the whole batch'}</strong>.
          {!targets.length && (
            <> Users already marked DONE are skipped by the engine, so this
            resumes rather than restarts.</>
          )}
        </>
      ),
      run: async (reason) => {
        // Unlike a real RBAC/subscription refusal (a non-2xx status,
        // already thrown by cpFetch below), a capacity refusal from
        // job_admission.py comes back as ok:false on an HTTP 200 -- the
        // same "ran, but didn't succeed" shape _gated() uses for any
        // other execution-time failure. Has to be checked explicitly here.
        const r = await startMigration(reason, ['drive'], targets, dryRun)
        if (!r.ok) throw new Error(r.detail || 'could not start')
      },
    })
  }

  const askStop = () => {
    if (!runningJob?.job_pid) return
    setPending({
      title: `Stop ${runningJob.active_job}`,
      description: (
        <>
          Sends <strong>SIGINT</strong> to pid {runningJob.job_pid} on{' '}
          {runningJob.node_id}. The engine finishes the item in flight and
          commits, so the ledger stays resumable — this is a pause, not a
          rollback. Nothing already copied is removed.
        </>
      ),
      run: async (reason) => { await stopJob(runningJob.job_pid!, reason) },
    })
  }

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, overflow: 'hidden' }}>
      <Box sx={{ p: 2, display: 'flex', flexWrap: 'wrap', gap: 2, alignItems: 'center' }}>
        <Typography variant="h6" sx={{ flexGrow: 1 }}>Job control</Typography>

        {/* Partial-failure state, stated rather than averaged. */}
        <Stack direction="row" spacing={1}>
          {(['RUNNING', 'DONE', 'FAILED', 'PENDING'] as const).map((s) =>
            counts[s] ? (
              <Chip key={s} size="small" label={`${counts[s]} ${s.toLowerCase()}`}
                    color={STATUS_COLOR[s]} variant={s === 'FAILED' ? 'filled' : 'outlined'} />
            ) : null,
          )}
        </Stack>
      </Box>

      <Box sx={{ px: 2, pb: 2, display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
        <ToggleButtonGroup size="small" exclusive value={filter}
                           onChange={(_, v) => v && setFilter(v)}>
          {(['all', 'RUNNING', 'FAILED', 'DONE', 'PENDING'] as const).map((f) => (
            <ToggleButton key={f} value={f} sx={{ textTransform: 'none' }}>{f}</ToggleButton>
          ))}
        </ToggleButtonGroup>

        <Box sx={{ flexGrow: 1 }} />

        <Button size="small" startIcon={<DryRunIcon />}
                onClick={() => askStart(true)}>Dry run</Button>
        <Button
            data-testid="action-migrate" size="small" variant="contained" startIcon={<StartIcon />}
                onClick={() => askStart(false)}>
          {selected.size ? `Migrate ${selected.size}` : 'Migrate all'}
        </Button>
        <Tooltip title={runningJob ? '' : 'Nothing running'}>
          <span>
            <Button size="small" color="error" variant="outlined" startIcon={<StopIcon />}
                    disabled={!runningJob} onClick={askStop}>Stop</Button>
          </span>
        </Tooltip>
      </Box>

      <TableContainer sx={{ maxHeight: 480 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell padding="checkbox" />
              <TableCell>User</TableCell>
              <TableCell>Status</TableCell>
              <TableCell sx={{ minWidth: 160 }}>Progress</TableCell>
              <TableCell align="right">Done</TableCell>
              <TableCell align="right">Failed</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {shown.map((u) => (
              <TableRow key={u.source_email} hover selected={selected.has(u.source_email)}>
                <TableCell padding="checkbox">
                  <Checkbox size="small" checked={selected.has(u.source_email)}
                            onChange={() => toggle(u.source_email)} />
                </TableCell>
                <TableCell>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>{u.source_email}</Typography>
                  <Typography variant="caption" color="text.secondary">→ {u.target_email}</Typography>
                </TableCell>
                <TableCell>
                  <Chip size="small" label={u.status} color={STATUS_COLOR[u.status]}
                        variant={u.status === 'FAILED' ? 'filled' : 'outlined'} />
                </TableCell>
                <TableCell>
                  <LinearProgress
                    variant="determinate" value={Math.min(100, u.percent)}
                    color={u.itemsFailed > 0 ? 'warning' : 'primary'}
                    sx={{ height: 6 }}
                  />
                  <Typography variant="caption" color="text.secondary">
                    {u.percent}% of attempted
                  </Typography>
                </TableCell>
                <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  {u.itemsDone.toLocaleString()}
                </TableCell>
                <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  {u.itemsFailed
                    ? <Typography variant="body2" color="error" fontWeight={700}>
                        {u.itemsFailed}
                      </Typography>
                    : '—'}
                </TableCell>
              </TableRow>
            ))}
            {!shown.length && (
              <TableRow>
                <TableCell colSpan={6}>
                  <Typography variant="body2" color="text.secondary"
                              sx={{ py: 3, textAlign: 'center' }}>
                    No users with status {filter}.
                  </Typography>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>

      <ReasonCodeDialog
        open={!!pending}
        title={pending?.title ?? ''}
        description={pending?.description}
        destructive={pending?.title.startsWith('Stop')}
        busy={busy}
        error={error}
        onCancel={() => { setPending(null); setError(null) }}
        onConfirm={submit}
      />
    </Paper>
  )
}

export default JobController
