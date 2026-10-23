/**
 * Everything one running job measures, behind a click on its card.
 *
 * The card is a glance: kind, tenant, progress, still-going. This is the
 * answer to "and how long is that actually going to take", which the card
 * deliberately does not try to fit.
 *
 * A seed prints a great deal that SeedRunDashboard already turns into
 * observed throughput, per-user detail and an ETA measured from the run
 * itself. Everything else -- setup, fleet migrations, another account's job
 * -- has only a percentage and a clock, so it gets an ETA derived from
 * those, clearly labelled as the projection it is rather than a measurement.
 */
import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Chip, CircularProgress, Dialog, DialogContent, DialogTitle, Divider,
  IconButton, LinearProgress, Stack, Table, TableBody, TableCell, TableContainer,
  TableHead, TableRow, Paper, Typography,
} from '@mui/material'
import { Close as CloseIcon } from '@mui/icons-material'
import type { RunningJob } from '@/hooks/useRunningJobs'
import { describeElapsed } from '@/hooks/useRunningJobs'
import SeedRunDashboard from '@/components/SeedRunDashboard'
import MigrateMetricsCharts from '@/components/MigrateMetricsCharts'
import { fetchMyMetrics, MetricsSnapshot } from '@/api/controlPlane'
import type { Movement } from '@/api/controlPlane'
import { bytes } from '@/utils/metricsSeries'
import { formatPct } from '@/utils/formatPct'
import { jobFacts, jobProgressPct, projectedEta, verificationEstimate, verificationMetrics } from './RunningJobDetail.utils'
import ProcessPanel from './ProcessPanel'

/** Fetched here, once, rather than inside the charts section below: the top
 *  "glance" row wants the same snapshot the charts render from, and fetching
 *  it twice would let the two disagree about what "now" means. */
const useMigrateMetrics = (enabled: boolean, live: boolean, users?: string[], since?: string) => {
  const [m, setM] = useState<MetricsSnapshot | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!enabled) return undefined
    let on = true
    // Scoped to this run when it names its users: otherwise every figure is the
    // whole account's ledger (live: 1.6M items "expected" for a one-user run).
    const load = () => fetchMyMetrics(120, users, since)
      .then((r) => { if (on) { setM(r); setErr('') } })
      .catch((e) => { if (on) setErr(e instanceof Error ? e.message : String(e)) })
    load()
    const t = live ? window.setInterval(load, 5_000) : undefined
    return () => { on = false; if (t) window.clearInterval(t) }
  }, [enabled, live, users?.join(','), since])  // eslint-disable-line react-hooks/exhaustive-deps
  return { m, err }
}

const Stat: React.FC<{ label: string; value: string; hint?: string }> = ({
  label, value, hint,
}) => (
  <Box sx={{ minWidth: 120 }}>
    <Typography variant="caption" color="text.secondary">{label}</Typography>
    <Typography variant="h6" sx={{ fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
      {value}
    </Typography>
    {hint && <Typography variant="caption" color="text.disabled">{hint}</Typography>}
  </Box>
)

const formatRate = (rate: number): string => {
  const digits = rate >= 10 ? 0 : rate >= 1 ? 1 : rate >= 0.1 ? 2 : 3
  return `${rate.toFixed(digits)} / min`
}

export const RunningJobDetail: React.FC<{
  job: RunningJob | null
  onClose: () => void
}> = ({ job, onClose }) => {
  // Called unconditionally (hooks can't follow the early return below): a job
  // that isn't a running migrate just never enables the fetch.
  const isMigrate = job?.kind === 'migrate'
  const { m, err: metricsErr } = useMigrateMetrics(isMigrate, isMigrate && !job?.done,
                                                   job?.users, job?.startedAt)
  if (!job) return null
  // Every job that has no dashboard of its own is measured from what it prints.
  const facts = !isMigrate && job.kind !== 'seed' && job.lines?.length
    ? jobFacts(job.lines, job.elapsedSec) : null
  const verifyEstimate = job.kind === 'verify'
    ? verificationEstimate(job.lines ?? [], job.elapsedSec) : null
  const verifyMetrics = job.kind === 'verify'
    ? verificationMetrics(job.lines ?? [], job.elapsedSec) : null
  const progressPct = verifyEstimate?.progressPct ?? jobProgressPct(job.label, job.pct, job.lines)
  const eta = verifyEstimate?.etaSec ?? facts?.etaSec ?? projectedEta(progressPct, job.elapsedSec)
  const t = m?.throughput
  return (
    <Dialog open onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pr: 6 }}>
        <Stack direction="row" alignItems="center" spacing={1}>
          <Chip size="small" label={job.kind} />
          <Typography variant="h6" sx={{ fontWeight: 700 }}>
            {job.domain || job.label}
          </Typography>
        </Stack>
        <IconButton onClick={onClose} size="small"
                    sx={{ position: 'absolute', right: 8, top: 12 }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {job.detail}
        </Typography>

        <Stack direction="row" flexWrap="wrap" gap={3} sx={{ mb: 2 }}>
          <Stat label="Elapsed"
                value={job.elapsedSec ? describeElapsed(job.elapsedSec) : '--'} />
          {/* "attempted" is not a hedge: a seed reporting 38% had finished
              nothing at all, every one of its 76 attempts a failure. A bar
              that reads as a success rate is the misreading to prevent. */}
          <Stat label="Progress"
                value={typeof progressPct === 'number' ? formatPct(progressPct) : '--'}
                hint={verifyEstimate ? 'estimated from completed service checks'
                  : typeof progressPct === 'number' ? 'of the work attempted'
                  : 'not reported'} />
          {verifyEstimate && (
            <Stat label="Service checks"
                  value={`${Math.floor(verifyEstimate.doneServices)} of ${verifyEstimate.totalServices}`}
                  hint="finished plus current item progress" />
          )}
          {facts && (
            <Stat label="Done"
                  value={job.kind !== 'verify' && facts.done !== null && facts.total !== null
                    ? `${facts.done.toLocaleString()} of ${facts.total.toLocaleString()}` : '--'}
                  hint={job.kind === 'verify' ? 'see verification metrics below'
                    : facts.total !== null ? 'counted by the job itself' : 'this job prints no count'} />
          )}
          {facts && (
            <Stat label="Rate"
                  value={job.kind === 'verify' ? '--'
                    : facts.perMinute !== null ? formatRate(facts.perMinute) : '--'}
                  hint={job.kind === 'verify' ? 'see item rate below'
                    : facts.perMinute !== null ? 'measured since it started' : 'nothing done yet'} />
          )}
          {verifyMetrics && (
            <>
              <Stat label="Users complete"
                    value={`${verifyMetrics.completedUsers} of ${verifyMetrics.usersExpected}`} />
              <Stat label="User concurrency"
                    value={verifyMetrics.activeUsers !== null
                      ? `${verifyMetrics.activeUsers} active / ${verifyMetrics.workers ?? '—'} workers`
                      : `${verifyMetrics.workers ?? '—'} workers`}
                    hint={`${verifyMetrics.pendingUsers ?? '—'} users waiting; auto-sized up to ${verifyMetrics.maxWorkers ?? '—'}`} />
              <Stat label="Items checked"
                    value={verifyMetrics.checked.toLocaleString()}
                    hint={verifyMetrics.inFlightItems
                      ? `includes ${verifyMetrics.inFlightItems.toLocaleString()} in active checks`
                      : 'finished service results'} />
              <Stat label="Identical"
                    value={verifyMetrics.identical.toLocaleString()}
                    hint="confirmed matches from finished services" />
              <Stat label="Differences"
                    value={verifyMetrics.differences.toLocaleString()}
                    hint="confirmed mismatches so far" />
              <Stat label="Missing"
                    value={verifyMetrics.missing.toLocaleString()}
                    hint="not found on target" />
              <Stat label="Read errors"
                    value={verifyMetrics.errors.toLocaleString()}
                    hint="incomplete checks from finished services" />
              <Stat label="Item rate"
                    value={verifyMetrics.itemsPerMinute !== null
                      ? formatRate(verifyMetrics.itemsPerMinute) : '--'}
                    hint="items examined per minute" />
            </>
          )}
          <Stat label={facts?.etaSec != null && !verifyEstimate ? 'ETA (measured)' : 'ETA (projected)'}
                value={eta != null ? describeElapsed(Math.round(eta)) : '--'}
                hint={eta != null ? verifyEstimate
                  ? 'early estimate; service sizes vary and it recalculates as checks finish'
                  : 'at the current rate'
                  : 'needs a count or a percentage'} />
          {facts && (
            <Stat label="Failures"
                  value={facts.failures.length.toLocaleString()}
                  hint={facts.failures.length ? 'lines the job reported as failed' : 'none reported'} />
          )}
          <Stat label="State"
                value={!job.done ? 'running'
                  : job.rc === 0 ? 'finished'
                  : job.rc == null ? 'finished'
                  : `exit ${job.rc}`}
                hint={!job.done ? undefined
                  : job.finishedAt
                    ? new Date(job.finishedAt * 1000).toLocaleString()
                    : 'no finish time recorded'} />
          {/* expectedBytes is discovery's own measured Drive walk, never estimated
              from item counts -- files vary from empty to gigabytes each, so an
              average would be fiction. Absent entirely (not a zero) when discovery
              has never run, same reasoning as the ETA above having no baseline. */}
          {isMigrate && t && t.expectedBytes > 0 && (
            /* Not "X of Y": copied counts mail, shared drives and files shared in from
               outside too; discovery counted only the users' own Drives. Live,
               dharan.gov.np read "114.6 GB of 103.5 GB" 8% of the way in. */
            <Stat label="Data copied"
                  value={bytes(t.bytesMovedTotal)}
                  hint={`mail and Drive, incl. shared in; users' own Drives held ${bytes(t.expectedBytes)} when counted`} />
          )}
        </Stack>

        {verifyMetrics && verifyMetrics.services.length > 0 && (
          <Box sx={{ mb: 2 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
              Verification by service
            </Typography>
            <TableContainer component={Paper} variant="outlined">
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Service</TableCell><TableCell>State</TableCell>
                    <TableCell align="right">Items in progress</TableCell>
                    <TableCell align="right">Checked</TableCell><TableCell align="right">Identical</TableCell>
                    <TableCell align="right">Differences</TableCell><TableCell align="right">Missing</TableCell>
                    <TableCell align="right">Read errors</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {verifyMetrics.services.map((s) => (
                    <TableRow key={s.service}>
                      <TableCell sx={{ textTransform: 'capitalize' }}>{s.service}</TableCell>
                      <TableCell>
                        {s.active ? `${s.active} active` : s.finished ? `${s.finished} finished` : 'queued'}
                      </TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {s.active ? `${s.activeItems.toLocaleString()}${s.activeTotal ? ` / ${s.activeTotal.toLocaleString()}` : ''}` : '—'}
                      </TableCell>
                      <TableCell align="right">{s.checked.toLocaleString()}</TableCell>
                      <TableCell align="right">{s.identical.toLocaleString()}</TableCell>
                      <TableCell align="right">{s.differences.toLocaleString()}</TableCell>
                      <TableCell align="right">{s.missing.toLocaleString()}</TableCell>
                      <TableCell align="right">{s.errors.toLocaleString()}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </Box>
        )}

        {isMigrate && m?.movement && <HowItMoves mv={m.movement} />}

        {facts && facts.warnings.length > 0 && (
          <Alert severity="warning" sx={{ mb: 2 }} data-testid="job-quota-warnings">
            <Typography variant="body2" sx={{ fontWeight: 700 }}>
              Google API rate limit reported ({facts.warnings.length} response{facts.warnings.length === 1 ? '' : 's'}).
            </Typography>
            <Typography variant="body2">
              The run logged quota errors. Check the completed count before deciding whether to retry.
            </Typography>
            {facts.warnings.slice(-2).map((w, i) => (
              <Typography key={i} variant="body2" sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                {w.trim()}
              </Typography>
            ))}
          </Alert>
        )}

        {/* An indeterminate bar means "working, can't say how far". On a
            run that already ended it means nothing at all, so a finished
            job with no percentage gets no bar rather than a perpetual one. */}
        {typeof progressPct === 'number'
          ? <LinearProgress variant="determinate" value={progressPct} />
          : !job.done ? <LinearProgress /> : null}

        <Divider sx={{ my: 2 }} />
        {!job.done && job.pid ? <ProcessPanel pid={job.pid} /> : null}
        {facts && facts.results.length > 0 && (
          <Alert severity={facts.failures.length ? 'warning' : 'success'} sx={{ mb: 2 }} data-testid="job-results">
            {facts.results.map((r, i) => (
              <Typography key={i} variant="body2" sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                {r.trim()}
              </Typography>
            ))}
          </Alert>
        )}
        {facts && facts.failures.length > 0 && (
          <Alert severity="error" sx={{ mb: 2 }} data-testid="job-failures">
            <Typography variant="body2" sx={{ fontWeight: 700 }}>
              {facts.failures.length.toLocaleString()} failure line(s){facts.failures.length > 8 ? ' — the last 8:' : ':'}
            </Typography>
            {facts.failures.slice(-8).map((f, i) => (
              <Typography key={i} variant="body2" sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                {f.trim()}
              </Typography>
            ))}
          </Alert>
        )}
        {/* A migration records far more than a percentage -- rates,
            latencies, limiter state, volume -- and all of it is on the
            metrics endpoint. */}
        {isMigrate && (
          <Box sx={{ mb: 2 }}>
            {metricsErr ? (
              <Alert severity="warning" sx={{ mb: 2 }}>Metrics unavailable: {metricsErr}</Alert>
            ) : !m ? (
              <CircularProgress size={18} />
            ) : m.error ? (
              <Typography variant="body2" color="text.secondary">{m.error}</Typography>
            ) : (
              <MigrateMetricsCharts m={m} />
            )}
          </Box>
        )}
        {/* A seed measures itself far better than a percentage can: observed
            writes per minute, per-user results, and an ETA from the run
            rather than from arithmetic. Every other kind of job prints
            nothing that dashboard can read, so showing only that dashboard
            meant a wipe with a full transcript rendered an empty panel --
            the transcript below is what those jobs actually have to say. */}
        {job.lines && job.lines.length > 0 ? (
          <>
            {job.kind === 'seed' && (
              <SeedRunDashboard lines={job.lines}
                                elapsedSec={job.elapsedSec ?? 0}
                                running={!job.done}
                                nodes={job.nodes} />
            )}
            <Box component="pre" sx={{
              fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
              overflowX: 'auto', maxHeight: 320, whiteSpace: 'pre-wrap',
              m: 0, mt: job.kind === 'seed' ? 1.5 : 0,
            }}>
              {job.lines.join('\n')}
            </Box>
          </>
        ) : (
          /* Distinguish "nothing yet" from "nothing ever". A running job
             that has printed nothing so far is normal; a finished one that
             printed nothing means the transcript is gone, and a reader
             deserves to be told which of those they are looking at. */
          <Typography variant="body2" color="text.secondary">
            {job.done
              ? 'No output recorded for this run.'
              : 'No output yet — this job has not printed anything since it started.'}
          </Typography>
        )}
      </DialogContent>
    </Dialog>
  )
}

const NAMES: Record<string, string> = { drive: 'Drive', gmail: 'mail', calendar: 'calendar', contacts: 'contacts',
                                         tasks: 'tasks', chat: 'chat' }

/** How Drive and mail are moving: the pass, each method in words, and counts against
 *  the source -- separate figures, never one blended percentage. */
const HowItMoves: React.FC<{ mv: Movement }> = ({ mv }) => {
  const of = (done: number, total: number) =>
    total ? `${done.toLocaleString()} of ${total.toLocaleString()}` : `${done.toLocaleString()} (source not counted)`
  const stage = mv.stage
    ? `Pass ${mv.stage.pass} of ${mv.stage.of}: ${mv.stage.services.split(',').map((x) => NAMES[x.trim()] ?? x.trim()).join(', ')}`
    : 'Pass not reported yet'
  return (
    <Box sx={{ mb: 2, p: 1.5, border: '1px solid', borderColor: 'divider', borderRadius: 1 }} data-testid="how-it-moves">
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>How it is moving</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }} data-testid="how-stage">{stage}</Typography>
      <Stack direction={{ xs: 'column', sm: 'row' }} gap={2}>
        <Box sx={{ flex: 1, minWidth: 0 }} data-testid="how-drive">
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            Drive · {of(mv.drive.filesCopied, mv.drive.filesOnSource)} files
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>{mv.drive.how}</Typography>
          {mv.drive.formsRebuilt > 0 && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              {mv.drive.formsRebuilt.toLocaleString()} Google file(s) rebuilt through their own API (Forms, and
              Sheets or Docs no route could carry)
            </Typography>
          )}
        </Box>
        <Box sx={{ flex: 1, minWidth: 0 }} data-testid="how-mail">
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            Mail · {of(mv.mail.messagesCopied, mv.mail.messagesOnSource)} messages
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>{mv.mail.how}</Typography>
        </Box>
      </Stack>
    </Box>
  )
}

export default RunningJobDetail
