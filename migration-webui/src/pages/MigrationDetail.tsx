import React, { useCallback, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
  RadioGroup,
  Radio,
  FormControlLabel,
} from '@mui/material'
import {
  Refresh as RefreshIcon, ArrowBack as BackIcon,
  ArrowForward as ArrowIcon, Update as DeltaIcon,
  Speed as MetricsIcon, HealthAndSafety as RepairIcon,
} from '@mui/icons-material'
import {
  fetchMigrationDetail, startDelta, startMigration, runRepair,
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
  /* A full pass over every user, for when the ledger has been reset and a
     delta would not do: delta asks the source what CHANGED in a window, so
     after a reset it re-copies only recent mail and events and leaves the
     rest unmigrated. There was no way to start one from here at all --
     "Start a new migration" on the list page opens the setup wizard for a
     new tenant pair, which is a different thing entirely. */
  const [askFull, setAskFull] = useState(false)
  const [mailBy, setMailBy] = useState('engine')
  const [fullBusy, setFullBusy] = useState(false)
  const [fullError, setFullError] = useState<string | null>(null)
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
      .then((r) => { setD(r); setRepair(r.repair ?? null); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
    // The survey now rides along in the detail payload, computed in the same
    // read as the headline counters. Fetched separately it drifted -- the
    // header said 382 failures while the panel beside it said 383, because
    // the two requests landed seconds apart on a run producing failures
    // continuously. Both were right; together they read as a bug.
  }, [id])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, 5000)
    return () => window.clearInterval(t)
  }, [refresh])

  /** Human age of a server timestamp. Seconds matter here: the whole point
   *  is distinguishing "a moment ago" from "fifteen seconds of migration
   *  ago", which on a fast run is hundreds of items. */
  const ageOf = (iso: string) => {
    const secs = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000))
    if (secs < 2) return 'just now'
    if (secs < 90) return `${secs}s ago`
    return `${Math.round(secs / 60)}m ago`
  }

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
        <Button size="small" variant="outlined" color="warning"
                data-testid="run-full"
                disabled={d?.running || fullBusy}
                onClick={() => setAskFull(true)}>
          {d?.running ? 'migration running' : 'Run full migration'}
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
            {/* These numbers are served from a short server-side cache. On a
                run moving tens of items a second that is a visible gap
                against the ledger, and unlabelled it reads as the counters
                being stuck. */}
            {d.asOf && (
              <Typography variant="caption" color="text.secondary"
                          data-testid="as-of">
                counted {ageOf(d.asOf)}
              </Typography>
            )}
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
                <Stat id="itemsskipped" label="items skipped"
                      value={p.itemsSkipped ?? 0} />
                <Stat id="itemsfailed" label="items failed" value={p.itemsFailed}
                      tone="error" />
              </Stack>
            </Paper>
          )}

          {/* Pressing Run delta changed nothing visible: it moves the same
              counters a finished migration already left on screen, and the
              first press finished in one second. Name the job and age it. */}
          {!!d.activeJobs?.length && (
            <Alert severity="info" sx={{ mb: 2 }} data-testid="active-job">
              {d.activeJobs.map(j => (
                <div key={`${j.jobName}-${j.pid ?? 0}`}>
                  <strong>{j.jobName === 'delta' ? 'Delta pass' : j.jobName}</strong>
                  {' '}running
                  {j.startedAt ? ` — started ${ageOf(j.startedAt)}` : ''}
                  {j.pid ? ` (pid ${j.pid})` : ''}
                  {/* This run's own numbers. The cumulative counters move by
                      a rounding error on a delta, so a run that was working
                      read exactly like one that was not. */}
                  {d.sinceRun && (
                    <span data-testid="since-run">
                      {' · '}{d.sinceRun.moved.toLocaleString()} moved
                      {d.sinceRun.skipped > 0 &&
                        ` · ${d.sinceRun.skipped.toLocaleString()} unchanged`}
                      {d.sinceRun.failed > 0 &&
                        ` · ${d.sinceRun.failed.toLocaleString()} failed`}
                      {' this run'}
                    </span>
                  )}
                </div>
              ))}
            </Alert>
          )}

          {/* The headline counters go still for hours: a user flips to DONE
              only when every service finishes, so 24 large mailboxes all
              mid-flight means done/running/pending never move while
              hundreds of thousands of items do. Watching that, the only
              honest conclusion is that the tool is stuck. */}
          {!!d.runningUsers?.length && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }}
                   data-testid="running-users">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                In flight now ({d.runningUsers.length})
              </Typography>
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>User</TableCell>
                      <TableCell align="right">Items this run</TableCell>
                      <TableCell>Working on</TableCell>
                      <TableCell align="right">Last wrote</TableCell>
                      <TableCell align="right">Running for</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {d.runningUsers.map(u => (
                      <TableRow key={u.sourceUser}
                                data-testid={`running-${u.sourceUser}`}>
                        <TableCell>{u.sourceUser.split('@')[0]}</TableCell>
                        <TableCell align="right"
                                   sx={{ fontVariantNumeric: 'tabular-nums' }}>
                          {u.items.toLocaleString()}
                        </TableCell>
                        <TableCell>{u.lastType}</TableCell>
                        <TableCell align="right">{ageOf(u.lastAt)}</TableCell>
                        <TableCell align="right">{ageOf(u.startedAt)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
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
              {/* Repair is backgrounded and used to report nothing at all:
                  the button returned instantly, the totals beside it stayed
                  put because they are counted from the ledger it is still
                  writing, and there was no way to tell a run in progress
                  from a run that did nothing. */}
              {repair.lastRun && (
                <Alert data-testid="repair-last-run" sx={{ mb: 1.5 }}
                       severity={repair.lastRun.error ? 'error'
                                 : repair.lastRun.running ? 'info' : 'success'}>
                  {repair.lastRun.running
                    ? `Repair is running, started ${ageOf(repair.lastRun.startedAt)}. The counts below are from before it finishes.`
                    : repair.lastRun.error
                      ? `The last repair stopped: ${repair.lastRun.error}`
                      : `Last repair ${ageOf(repair.lastRun.finishedAt || repair.lastRun.startedAt)}: ${repair.lastRun.summary || 'nothing needed fixing'}`}
                </Alert>
              )}
              {!!repair.brokenFolders?.folders && (
                <Alert severity="warning" sx={{ mb: 1.5 }}
                       data-testid="broken-folders">
                  {/* "inside it" refers to the FOLDER, so it agrees with the
                      folder count -- keying it off the file count produced
                      "1 folder share failed, and 60 files inside them". */}
                  <strong>{repair.brokenFolders.folders.toLocaleString()} folder
                  share{repair.brokenFolders.folders === 1 ? '' : 's'} failed</strong>,
                  and{' '}
                  {repair.brokenFolders.files_behind.toLocaleString()} file
                  {repair.brokenFolders.files_behind === 1 ? '' : 's'} inside
                  {repair.brokenFolders.folders === 1 ? ' it rely' : ' them rely'}
                  {' '}on that share for access. Repair fixes folders first,
                  because one folder can restore hundreds of files at once.
                </Alert>
              )}
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

          {d.skipped && d.skipped.length > 0 && (
            <Paper variant="outlined" sx={{ p: 2, mb: 3 }}
                   data-testid="skipped-panel">
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
                What was deliberately skipped
              </Typography>
              {/* Separate from failures on purpose. A skip is a decision the
                  tool made -- a draft it will not insert, a doc past the
                  export ceiling, a grant it resolved as no longer failing --
                  and folding them into the failure count is how a clean run
                  teaches people to ignore red. */}
              <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
                {d.skipped.map((s) => (
                  <Chip key={s.status} size="small" variant="outlined"
                        data-testid={`skip-${s.status}`}
                        label={`${s.status.replace(/^SKIPPED_?/, '').toLowerCase()
                          .replace(/_/g, ' ') || 'skipped'} · ${s.count.toLocaleString()}`} />
                ))}
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
        open={askFull}
        busy={fullBusy}
        error={fullError}
        title={`Run a full migration over ${d?.sourceDomain || 'this tenant'}`}
        description={
          <>
            Copies everything the ledger does not already record, for all
            {' '}{d?.progress?.users ?? 0} users. Use this after a ledger
            reset, where a delta would not do: a delta asks the source what
            changed in a short window, so it would re-copy only recent mail
            and events and leave everything older unmigrated. Anything still
            in the ledger is skipped, so this is safe to re-run.
            {/* Mail was 349,560 of 593,816 items in the last real run --
                58.9% -- and it is the only service behind a ceiling that
                cannot be raised (3 sustained writes/sec/account, which
                Google states is not adjustable). Handing it to Google's own
                Data Migration Service sidesteps that limit instead of
                pacing against it. Offered as a choice, not a default: DMS
                gives per-user console status, not the per-item ledger that
                makes a re-run here idempotent. */}
            <Box sx={{ mt: 2 }}>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
                Who moves the mail?
              </Typography>
              <RadioGroup value={mailBy}
                          onChange={(e) => setMailBy(e.target.value)}>
                <FormControlLabel
                  value="engine" control={<Radio size="small" />}
                  data-testid="mail-by-engine"
                  label={
                    <Typography variant="body2">
                      <strong>This tool</strong> — full per-item ledger, exact
                      failure accounting, idempotent re-runs. Paced against
                      3 writes/sec/account, so mail sets the run&apos;s length.
                    </Typography>
                  } />
                <FormControlLabel
                  value="dms" control={<Radio size="small" />}
                  data-testid="mail-by-dms"
                  label={
                    <Typography variant="body2">
                      <strong>Google Data Migration Service</strong> — moves
                      mail inside Google, spending none of this
                      project&apos;s Gmail quota. Set it up in the Admin
                      console (Nodes → Deploy has the helper); this run then
                      migrates everything <em>except</em> mail, so nothing is
                      copied twice. You give up the per-item ledger for mail.
                    </Typography>
                  } />
              </RadioGroup>
            </Box>
          </>
        }
        onCancel={() => { setAskFull(false); setFullError(null) }}
        onConfirm={async (reason: string) => {
          setFullBusy(true); setFullError(null)
          try {
            // Excluding mail is the whole point of choosing DMS: running
            // both would insert every message twice, and the ledger cannot
            // see what Google moved internally.
            const services = mailBy === 'dms'
              ? ['drive', 'calendar', 'contacts', 'tasks', 'chat']
              : ['all']
            const r = await startMigration(reason, services, [], false,
                                           Number(accountId))
            if (!r.ok) throw new Error(r.detail || 'could not start')
            setAskFull(false)
            setStarted(r.detail || 'migration started')
            refresh()
          } catch (e: any) {
            setFullError(e.message)
          } finally {
            setFullBusy(false)
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
            const r = await startDelta(reason, deltaDays, Number(accountId))
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
