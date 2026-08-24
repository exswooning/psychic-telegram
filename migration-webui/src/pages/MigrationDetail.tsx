import React, { useCallback, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material'
import {
  Refresh as RefreshIcon, ArrowBack as BackIcon,
  ArrowForward as ArrowIcon, Update as DeltaIcon,
  Speed as MetricsIcon, HealthAndSafety as RepairIcon,
} from '@mui/icons-material'
import {
  fetchMigrationDetail, startDelta, fetchRepairSurvey, runRepair,
  MigrationDetail as Detail, RepairSurvey,
} from '@/api/controlPlane'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

/**
 * One migration in full: what moved, what failed, and why.
 *
 * Failures are grouped by cause, not listed per item. A run that fails 50
 * contacts fails them for ONE reason, and fifty identical HTTP 400s scrolled
 * down a page hides that completely -- the count and one example are what
 * anybody acts on. Affected mailboxes are named per cause because "which
 * users" is the next question every single time.
 */

const Stat: React.FC<{ id: string; label: string; value: number; tone?: 'error' }> =
  ({ id, label, value, tone }) => (
    <Box data-testid={`stat-${id}`}>
      <Typography sx={{ fontWeight: 700, fontSize: 22, lineHeight: 1.2,
                        fontVariantNumeric: 'tabular-nums',
                        color: tone === 'error' && value > 0
                          ? 'error.main' : 'text.primary' }}>
        {value.toLocaleString()}
      </Typography>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Box>
  )

export const MigrationDetail: React.FC = () => {
  const { accountId } = useParams()
  const navigate = useNavigate()
  const [d, setD] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [askDelta, setAskDelta] = useState(false)
  const [deltaBusy, setDeltaBusy] = useState(false)
  const [deltaError, setDeltaError] = useState<string | null>(null)
  const [deltaDays, setDeltaDays] = useState(2)
  const [started, setStarted] = useState('')
  const [repair, setRepair] = useState<RepairSurvey | null>(null)
  const [askRepair, setAskRepair] = useState(false)
  const [repairBusy, setRepairBusy] = useState(false)
  const [repairError, setRepairError] = useState<string | null>(null)

  const id = Number(accountId)

  const refresh = useCallback(() => {
    if (!Number.isFinite(id)) return
    setLoading(true)
    fetchMigrationDetail(id)
      .then((r) => { setD(r); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
    // Separate from the detail poll: a failure total means very little until
    // it is broken into causes, and most of this one usually needs nobody.
    fetchRepairSurvey(id).then(setRepair).catch(() => setRepair(null))
  }, [id])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, 5000)
    return () => window.clearInterval(t)
  }, [refresh])

  const p = d?.progress

  // A failure predating the current run is a queued retry, not a live
  // problem. Unknown timestamps (ledgers written before the column existed)
  // are deliberately NOT treated as stale: guessing "old" would hide a real
  // failure, and the two errors are not symmetric.
  const isStale = (u: { statusAt?: string }) =>
    !!d?.runStartedAt && !!u.statusAt && u.statusAt < d.runStartedAt
  const staleCount = (d?.failedUsers || []).filter(isStale).length

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <Button size="small" startIcon={<BackIcon />}
                onClick={() => navigate('/migrations')}
                data-testid="back">
          Migrations
        </Button>
        {loading && <CircularProgress size={16} />}
        <Box sx={{ flex: 1 }} />
        <TextField size="small" type="number" label="days"
                   value={deltaDays} sx={{ width: 90 }}
                   onChange={(e) => setDeltaDays(Math.max(1, Number(e.target.value) || 1))}
                   inputProps={{ 'data-testid': 'delta-days', min: 1, max: 90 }} />
        <Button size="small" startIcon={<MetricsIcon />}
                data-testid="open-metrics"
                onClick={() => navigate(`/migrations/${id}/metrics`)}>
          Performance
        </Button>
        <Button size="small" variant="outlined" startIcon={<DeltaIcon />}
                data-testid="run-delta"
                disabled={d?.running || deltaBusy}
                onClick={() => setAskDelta(true)}>
          {d?.running ? 'migration running' : 'Run delta'}
        </Button>
        <IconButton size="small" onClick={refresh} aria-label="refresh">
          <RefreshIcon fontSize="small" />
        </IconButton>
      </Stack>

      {started && (
        <Alert severity="success" sx={{ mb: 2 }} data-testid="delta-started">
          {started}
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {d?.error && <Alert severity="warning" sx={{ mb: 2 }}>{d.error}</Alert>}

      {d && (
        <>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
            <Typography variant="h5" sx={{ fontWeight: 700 }}>
              {d.sourceDomain || '—'}
            </Typography>
            <ArrowIcon color="disabled" />
            <Typography variant="h5" sx={{ fontWeight: 700 }}>
              {d.targetDomain || '—'}
            </Typography>
            <Chip size="small" label={d.running ? 'running' : 'idle'}
                  color={d.running ? 'primary' : 'default'}
                  variant={d.running ? 'filled' : 'outlined'} />
          </Stack>

          {p && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
              <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
                <Stat id="users" label="users" value={p.users} />
                <Stat id="done" label="done" value={p.done} />
                <Stat id="running" label="running" value={p.running} />
                <Stat id="pending" label="pending" value={p.pending} />
                <Stat id="failed" label="users failed" value={p.failed} tone="error" />
                <Stat id="blocked" label="blocked" value={p.blocked ?? 0} />
                <Stat id="items" label="items migrated" value={p.items} />
                <Stat id="itemsfailed" label="items failed" value={p.itemsFailed}
                      tone="error" />
              </Stack>
            </Paper>
          )}

          {repair && repair.total > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }}
                   data-testid="repair-panel">
              <Stack direction="row" alignItems="center" spacing={1}
                     sx={{ mb: 1 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                  What the {repair.total.toLocaleString()} failures are
                </Typography>
                <Box sx={{ flex: 1 }} />
                <Button size="small" variant="outlined" startIcon={<RepairIcon />}
                        data-testid="run-repair" disabled={repairBusy || d.running}
                        onClick={() => setAskRepair(true)}>
                  {d.running ? 'runs when the migration finishes' : 'Repair'}
                </Button>
              </Stack>
              {/* A total is not a diagnosis. Live, 119,600 failures were one
                  live bug and two families describing states that had since
                  stopped being true -- about six times the number that
                  actually needed anybody. */}
              <Stack spacing={1}>
                {repair.families.map((f) => (
                  <Stack key={f.key} direction="row" spacing={1}
                         alignItems="baseline"
                         data-testid={`repair-${f.key}`}>
                    <Typography sx={{ fontWeight: 700, minWidth: 84,
                                      textAlign: 'right',
                                      fontVariantNumeric: 'tabular-nums' }}>
                      {f.count.toLocaleString()}
                    </Typography>
                    <Typography variant="body2">{f.label}</Typography>
                    <Chip size="small" variant="outlined" label={f.fix} />
                  </Stack>
                ))}
                {repair.unclassified > 0 && (
                  <Stack direction="row" spacing={1} alignItems="baseline"
                         data-testid="repair-unclassified">
                    <Typography sx={{ fontWeight: 700, minWidth: 84,
                                      textAlign: 'right',
                                      fontVariantNumeric: 'tabular-nums' }}>
                      {repair.unclassified.toLocaleString()}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      not yet classified — these need a person
                    </Typography>
                  </Stack>
                )}
              </Stack>
            </Paper>
          )}

          <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              What moved
            </Typography>
            {d.items.length === 0 ? (
              <Typography variant="body2" color="text.secondary"
                          data-testid="nothing-moved">
                Nothing migrated yet for this pair.
              </Typography>
            ) : (
              <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
                {d.items.map((it) => (
                  <Chip key={it.type} size="small" variant="outlined"
                        data-testid={`item-${it.type}`}
                        label={`${it.type} · ${it.count.toLocaleString()}`} />
                ))}
              </Stack>
            )}
          </Paper>

          {d.failedUsers.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }}
                   data-testid="failed-users">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                Users that did not migrate ({d.failedUsers.length})
              </Typography>
              {/* A failure recorded before this run started is not a
                  current failure -- it is a queued retry. Rendering the two
                  identically showed 160 users as broken, with 18-hour-old
                  "invalid_grant" text against target accounts that had since
                  been recreated, while the run retrying them was healthy. */}
              {staleCount > 0 && (
                <Alert severity="info" sx={{ mb: 1.5 }} data-testid="stale-note">
                  {staleCount} of these failed in an earlier run and are
                  queued to be retried by the one in progress. They are
                  marked <strong>earlier run</strong> below.
                </Alert>
              )}
              {/* "blocked" and "failed" need opposite responses -- one is
                  waited on, the other investigated. Labelling them the same
                  trains people to skim a list meant to demand attention. */}
              <Stack spacing={1.5}>
                {d.failedUsers.map((u) => (
                  <Box key={u.sourceUser} data-testid={`faileduser-${u.sourceUser}`}>
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Typography variant="body2" sx={{ fontWeight: 600 }}>
                        {u.sourceUser}
                      </Typography>
                      <Chip size="small"
                            color={u.status === 'BLOCKED' ? 'warning' : 'error'}
                            variant={u.status === 'BLOCKED' || isStale(u)
                              ? 'outlined' : 'filled'}
                            label={u.status === 'BLOCKED'
                              ? 'blocked — waiting on you' : 'failed'} />
                      {isStale(u) && (
                        <Chip size="small" variant="outlined" color="info"
                              data-testid={`stale-${u.sourceUser}`}
                              label="earlier run — queued for retry" />
                      )}
                    </Stack>
                    <Typography variant="caption" color="text.secondary"
                                sx={{ whiteSpace: 'pre-wrap' }}>
                      {u.detail || 'no detail recorded'}
                    </Typography>
                  </Box>
                ))}
              </Stack>
            </Paper>
          )}

          <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="users-table">
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              Users ({d.users.length.toLocaleString()})
            </Typography>
            {/* Failures first. A 200-row table sorted alphabetically buries
                the two rows anybody opened this page to find. */}
            <Box sx={{ maxHeight: 420, overflowY: 'auto' }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell>user</TableCell>
                    <TableCell>target</TableCell>
                    <TableCell>state</TableCell>
                    <TableCell>services done</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {d.users.map((u) => (
                    <TableRow key={u.sourceUser}
                              data-testid={`user-${u.sourceUser}`}>
                      <TableCell sx={{ fontSize: 12 }}>{u.sourceUser}</TableCell>
                      <TableCell sx={{ fontSize: 12 }}>{u.targetUser}</TableCell>
                      <TableCell>
                        <Chip size="small"
                              variant={u.status === 'DONE' ? 'outlined' : 'filled'}
                              color={u.status === 'DONE' ? 'success'
                                     : u.status === 'FAILED' ? 'error'
                                     : u.status === 'RUNNING' ? 'primary' : 'default'}
                              label={u.status.toLowerCase()} />
                      </TableCell>
                      <TableCell sx={{ fontSize: 11 }}>
                        {u.services || '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          </Paper>

          <Paper variant="outlined" sx={{ p: 2 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              Errors, grouped by cause
            </Typography>
            {d.failures.length === 0 ? (
              <Typography variant="body2" color="text.secondary"
                          data-testid="no-failures">
                No failures recorded.
              </Typography>
            ) : (
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>count</TableCell>
                      <TableCell>type</TableCell>
                      <TableCell>cause</TableCell>
                      <TableCell>affected users</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {d.failures.map((f, i) => (
                      <TableRow key={`${f.itemType}-${i}`}
                                data-testid={`failure-${i}`}>
                        <TableCell sx={{ fontWeight: 700 }}>
                          {f.count.toLocaleString()}
                        </TableCell>
                        <TableCell sx={{ fontSize: 12 }}>{f.itemType}</TableCell>
                        <TableCell sx={{ fontSize: 12, maxWidth: 520 }}>
                          {f.reason}
                        </TableCell>
                        <TableCell sx={{ fontSize: 11 }}>
                          {f.users.join(', ')}
                          {f.userCount > f.users.length
                            && ` +${(f.userCount - f.users.length).toLocaleString()} more`}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
          </Paper>
        </>
      )}
      <ReasonCodeDialog
        open={askRepair}
        busy={repairBusy}
        error={repairError}
        title="Repair what can be repaired"
        description={
          <>
            Re-checks each failure against the target and clears the ones that
            describe a state which is no longer true — a share refused because
            the person had no account when it was tried, or a grant the target
            turns out to hold already. Nothing is deleted: the audit row keeps
            its original error. Failures that are still real stay exactly where
            they are. This also runs automatically at the end of every
            migration.
          </>
        }
        onCancel={() => { setAskRepair(false); setRepairError(null) }}
        onConfirm={async (reason: string) => {
          setRepairBusy(true); setRepairError(null)
          try {
            const r = await runRepair(id, reason)
            if (!r.ok) throw new Error(r.detail || 'could not start')
            setAskRepair(false)
            setStarted(r.detail || 'repair started')
            refresh()
          } catch (e: any) {
            setRepairError(e.message)
          } finally {
            setRepairBusy(false)
          }
        }}
      />
      <ReasonCodeDialog
        open={askDelta}
        busy={deltaBusy}
        error={deltaError}
        title={`Run a delta pass over ${d?.sourceDomain || 'this tenant'}`}
        description={
          <>
            Re-asks the source what changed in the last {deltaDays} day(s) and
            copies it, rather than re-copying what is already in the ledger.
            This is the pass you run repeatedly between a bulk copy and a
            cutover, and once more after the cutover window closes. It uses
            the same engine and the same machine-wide capacity slot as a full
            migration.
          </>
        }
        onCancel={() => { setAskDelta(false); setDeltaError(null) }}
        onConfirm={async (reason: string) => {
          setDeltaBusy(true); setDeltaError(null)
          try {
            const r = await startDelta(reason, deltaDays)
            if (!r.ok) throw new Error(r.detail || 'could not start')
            setAskDelta(false)
            setStarted(r.detail || 'delta pass started')
            refresh()
          } catch (e: any) {
            setDeltaError(e.message)
          } finally {
            setDeltaBusy(false)
          }
        }}
      />
    </Box>
  )
}

export default MigrationDetail
