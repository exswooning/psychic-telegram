import React from 'react'
import {
  Box, Chip, LinearProgress, Stack, Table, TableBody, TableCell, TableHead,
  TableRow, Tooltip, Typography, TableContainer, Paper,
} from '@mui/material'
import { parseSeedRun, SeedRun } from '@/utils/seedLog'

/**
 * Everything a seed run actually measures, in one place.
 *
 * seed_sandbox.py prints a great deal that the UI used to discard: its own
 * scale/worker sizing, its own estimate of total API writes and minutes,
 * a fully itemised per-user result, and a warning line per partially
 * failed service. Showing a percentage and a wall of raw text over the top
 * of all that is what made the UI feel like it wasn't reflecting the real
 * work -- the data was there, it just never reached the screen.
 *
 * Two rules hold throughout, both learned from this codebase's own
 * history (see tui.py's IA notes and CoverageAudit's ABSENT/UNPROBED):
 *   * a number that was never printed renders as "--", never as 0 -- "not
 *     reported" and "reported as zero" are different facts, and a seed
 *     legitimately produces real zeros (0 contacts when People API is off);
 *   * anything this component computes rather than reads is labelled as
 *     observed, and shown beside the run's own estimate rather than
 *     replacing it.
 */

const COUNT_ORDER = [
  'files', 'folders', 'comments', 'messages', 'drafts', 'events',
  'secondary calendars', 'chat messages', 'spaces', 'contacts', 'tasks',
]

// The User column carries an email plus a department/project line, both
// noWrap. Unbounded, auto table layout sized it to the longest of those --
// measured at 2,513px against a 1,256px container, which pushed all
// fourteen remaining columns (Status, Took, every count, and Failed) off
// screen behind 2,450px of horizontal scroll with no visible affordance.
// The one column identifying a failed user was the furthest out of reach.
const USER_COL = 260

const fmt = (n: number) => n.toLocaleString()

const dur = (sec: number): string => {
  if (sec < 60) return `${Math.round(sec)}s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m}m ${Math.round(sec % 60)}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

const Stat: React.FC<{
  label: string; value: React.ReactNode; hint?: string; accent?: boolean
}> = ({ label, value, hint, accent }) => (
  // data-testid: several of these labels ("files", "contacts", ...) are
  // also table column headers, so tests need to address the tile itself
  // rather than any element that happens to contain the same word.
  <Box data-testid={`stat-${label}`} sx={{
    px: 1.5, py: 1, borderRadius: 1, bgcolor: 'action.hover',
    minWidth: 104, flex: '1 1 104px',
  }}>
    <Typography variant="caption" color="text.secondary" sx={{
      display: 'block', textTransform: 'uppercase', letterSpacing: 0.4, fontSize: 10,
    }}>
      {label}
    </Typography>
    <Tooltip title={hint ?? ''} placement="top" disableHoverListener={!hint}>
      <Typography variant="body2" sx={{
        fontWeight: 700, fontVariantNumeric: 'tabular-nums',
        color: accent ? 'primary.main' : 'text.primary',
        cursor: hint ? 'help' : 'default',
      }}>
        {value}
      </Typography>
    </Tooltip>
  </Box>
)

const SeedRunDashboard: React.FC<{
  lines: string[]; elapsedSec?: number; running?: boolean
}> = ({ lines, elapsedSec, running }) => {
  const run: SeedRun = React.useMemo(() => parseSeedRun(lines), [lines])

  if (run.users.length === 0 && run.totalUsers === undefined) return null

  // A user with a "starting" line and no "done" line means two opposite
  // things depending on whether the process is still alive. While the run
  // is live it is genuinely in flight. Once the run has exited it is a
  // user that FAILED -- seed_sandbox.py prints its own "PARTIAL: N of M
  // users seeded" line for exactly this case. Rendering it as "in flight"
  // on a dead run is how the single failed user of a 201-user seed came to
  // be the one user the dashboard said nothing about.
  const ended = running === false
  const stranded = ended ? run.runningCount : 0

  const pct = run.totalUsers ? Math.round((run.doneCount / run.totalUsers) * 100) : null

  // A rate is only meaningful once at least one full parallel batch has
  // landed. seed_sandbox runs `workers` users concurrently, so they finish
  // in clumps: with 9 workers and 1 user done, elapsed covers that user's
  // whole runtime but only 1/9th of the work actually completed in it.
  // Extrapolating there produced a confident "300h" against the run's own
  // 11h44m estimate -- wrong by 25x, and shown in accent colour as though
  // it were the reliable figure. Same rule as everywhere else here: no
  // baseline means "--", not a fabricated number.
  const sampleNeeded = Math.max(run.workers ?? 1, 2)
  const haveSample = run.doneCount >= sampleNeeded
  const perMin = haveSample && elapsedSec && elapsedSec > 0
    ? run.doneCount / (elapsedSec / 60) : null
  const remaining = run.totalUsers != null ? run.totalUsers - run.doneCount : null
  const etaSec = perMin && remaining != null && remaining > 0 ? (remaining / perMin) * 60 : null
  const sampleHint = haveSample ? undefined
    : `Needs ${sampleNeeded} finished users before this means anything -- `
      + `${run.workers ?? '?'} run in parallel, so they finish in batches `
      + `(${run.doneCount} done so far)`

  const counts = COUNT_ORDER.filter((k) => k in run.totals)
  const totalWarnings = run.warnings.reduce((s, w) => s + w.count, 0)

  return (
    <Box sx={{ mt: 1 }}>
      {/* Run identity + sizing, straight from the run's own banner. */}
      <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1, mb: 1 }}>
        <Stat label="Users" value={run.totalUsers != null
          ? `${fmt(run.doneCount)} / ${fmt(run.totalUsers)}` : fmt(run.doneCount)}
          hint="Finished / total, counted from the run's own per-user lines" />
        {ended ? (
          <Stat label="Never finished" value={stranded || '--'} accent={stranded > 0}
                hint={stranded > 0
                  ? `${stranded} user(s) started and produced no result line before the run `
                    + `exited -- they failed. See the per-user table and the '!' lines in the log.`
                  : 'Every user that started also finished'} />
        ) : (
          <Stat label="In flight" value={run.runningCount || '--'} accent={run.runningCount > 0}
                hint="Users started but not yet finished" />
        )}
        <Stat label="Workers" value={run.workers ?? '--'} hint={run.workerReason} />
        <Stat label="Scale" value={run.scale ?? '--'} />
        <Stat label="Elapsed" value={elapsedSec != null ? dur(elapsedSec) : '--'} />
        <Stat label="Observed rate"
              value={perMin ? `${perMin.toFixed(2)}/min` : '--'}
              hint={sampleHint
                ?? "Users finished per minute so far -- measured, not the run's estimate"} />
        <Stat label="ETA (observed)" value={ended ? '--' : etaSec != null ? dur(etaSec) : '--'}
              accent={!ended && etaSec != null}
              hint={ended ? 'The run has finished -- there is nothing left to estimate'
                : sampleHint
                ?? 'Extrapolated from the observed rate above, not from the run’s up-front estimate'} />
        <Stat label="Est. at start"
              value={run.estimatedMinutes != null ? dur(run.estimatedMinutes * 60) : '--'}
              hint={run.estimatedWrites != null
                ? `The run's own up-front estimate: ~${fmt(run.estimatedWrites)} API writes`
                : "The run's own up-front estimate"} />
      </Stack>

      {pct != null && (
        <Box sx={{ mb: 1.5 }}>
          <LinearProgress variant="determinate" value={pct} sx={{ height: 6, borderRadius: 3 }} />
          <Typography variant="caption" color="text.secondary">
            {pct}% of users finished{ended && stranded > 0 && ` · ${fmt(stranded)} failed`}
            {run.domain && ` · ${run.domain}`}
            {run.externalCollaborator && ` · external collaborator ${run.externalCollaborator}`}
          </Typography>
        </Box>
      )}

      {/* What has actually been created so far, itemised. */}
      {counts.length > 0 && (
        <>
          <Typography variant="caption" color="text.secondary" sx={{
            display: 'block', mb: 0.5, fontWeight: 600,
          }}>
            {ended ? 'Created' : 'Created so far'} (summed across {fmt(run.doneCount)} finished
            user{run.doneCount === 1 ? '' : 's'})
          </Typography>
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1, mb: 1.5 }}>
            {counts.map((k) => (
              <Stat key={k} label={k} value={fmt(run.totals[k])} />
            ))}
          </Stack>
        </>
      )}

      {/* Warnings, grouped -- the raw log repeats these hundreds of times. */}
      {run.warnings.length > 0 && (
        <>
          <Typography variant="caption" color="text.secondary" sx={{
            display: 'block', mb: 0.5, fontWeight: 600,
          }}>
            Warnings ({fmt(totalWarnings)} total, {run.warnings.length} distinct)
          </Typography>
          <Stack spacing={0.5} sx={{ mb: 1.5 }}>
            {run.warnings.map((w) => (
              <Tooltip key={`${w.kind}-${w.code}`} title={w.sample} placement="top-start">
                <Stack direction="row" spacing={1} alignItems="center" sx={{
                  px: 1.5, py: 0.5, borderRadius: 1, bgcolor: 'action.hover', cursor: 'help',
                }}>
                  <Chip size="small" label={`${w.count}x`} color="warning" variant="outlined"
                        sx={{ fontVariantNumeric: 'tabular-nums', minWidth: 56 }} />
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>{w.kind}</Typography>
                  <Typography variant="caption" color="text.secondary" noWrap>
                    {w.code ?? 'no HTTP code reported'}
                  </Typography>
                </Stack>
              </Tooltip>
            ))}
          </Stack>
        </>
      )}

      {/* Per-user detail -- in-flight first, that being the live question. */}
      <Typography variant="caption" color="text.secondary" sx={{
        display: 'block', mb: 0.5, fontWeight: 600,
      }}>
        Per user
      </Typography>
      <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 320, borderRadius: 1 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 600, width: USER_COL, maxWidth: USER_COL }}>
                User
              </TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
              <TableCell sx={{ fontWeight: 600 }} align="right">Took</TableCell>
              {COUNT_ORDER.filter((k) => counts.includes(k)).map((k) => (
                <TableCell key={k} sx={{ fontWeight: 600, whiteSpace: 'nowrap' }} align="right">
                  {k}
                </TableCell>
              ))}
              <TableCell sx={{ fontWeight: 600 }}>Failed</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {[...run.users]
              .sort((a, b) => (a.status === 'running' ? 0 : 1) - (b.status === 'running' ? 0 : 1))
              .map((u) => (
                <TableRow key={u.email} hover>
                  <TableCell sx={{ width: USER_COL, maxWidth: USER_COL }}>
                    <Typography variant="body2" title={u.email} noWrap
                                sx={{ fontWeight: 600, maxWidth: USER_COL }}>
                      {u.email}
                    </Typography>
                    {u.context && (
                      <Typography variant="caption" color="text.secondary" noWrap
                                  title={u.context} sx={{ display: 'block', maxWidth: USER_COL }}>
                        {u.context}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    <Chip size="small"
                          label={u.status !== 'running' ? u.status
                            : ended ? 'never finished' : 'in flight'}
                          color={u.status !== 'running' ? 'success'
                            : ended ? 'error' : 'info'}
                          variant={u.status === 'running' && !ended ? 'filled' : 'outlined'} />
                  </TableCell>
                  <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                    {u.elapsedSec != null ? dur(u.elapsedSec) : '--'}
                  </TableCell>
                  {COUNT_ORDER.filter((k) => counts.includes(k)).map((k) => (
                    <TableCell key={k} align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                      {u.counts && k in u.counts ? fmt(u.counts[k]) : '--'}
                    </TableCell>
                  ))}
                  <TableCell>
                    {u.failedServices.length > 0
                      ? u.failedServices.map((s) => (
                          <Chip key={s} size="small" label={s} color="error"
                                variant="outlined" sx={{ mr: 0.5 }} />
                        ))
                      : ended && u.status === 'running'
                      ? <Chip size="small" label="no result line" color="error"
                              variant="outlined" />
                      : <Typography variant="caption" color="text.secondary">--</Typography>}
                  </TableCell>
                </TableRow>
              ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  )
}

export default SeedRunDashboard
