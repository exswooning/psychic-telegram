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
  IconButton, LinearProgress, Stack, Typography,
} from '@mui/material'
import { Close as CloseIcon } from '@mui/icons-material'
import type { RunningJob } from '@/hooks/useRunningJobs'
import { describeElapsed } from '@/hooks/useRunningJobs'
import SeedRunDashboard from '@/components/SeedRunDashboard'
import MigrateMetricsCharts from '@/components/MigrateMetricsCharts'
import { fetchMyMetrics, MetricsSnapshot } from '@/api/controlPlane'
import { bytes } from '@/utils/metricsSeries'
import { formatPct } from '@/utils/formatPct'
import { projectedEta } from './RunningJobDetail.utils'

/** Fetched here, once, rather than inside the charts section below: the top
 *  "glance" row wants the same snapshot the charts render from, and fetching
 *  it twice would let the two disagree about what "now" means. */
const useMigrateMetrics = (enabled: boolean, live: boolean) => {
  const [m, setM] = useState<MetricsSnapshot | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!enabled) return undefined
    let on = true
    const load = () => fetchMyMetrics(120)
      .then((r) => { if (on) { setM(r); setErr('') } })
      .catch((e) => { if (on) setErr(e instanceof Error ? e.message : String(e)) })
    load()
    const t = live ? window.setInterval(load, 5_000) : undefined
    return () => { on = false; if (t) window.clearInterval(t) }
  }, [enabled, live])
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

export const RunningJobDetail: React.FC<{
  job: RunningJob | null
  onClose: () => void
}> = ({ job, onClose }) => {
  // Called unconditionally (hooks can't follow the early return below): a job
  // that isn't a running migrate just never enables the fetch.
  const isMigrate = job?.kind === 'migrate'
  const { m, err: metricsErr } = useMigrateMetrics(isMigrate, isMigrate && !job?.done)
  if (!job) return null
  const eta = projectedEta(job.pct, job.elapsedSec)
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
                value={typeof job.pct === 'number' ? formatPct(job.pct) : '--'}
                hint={typeof job.pct === 'number' ? 'of the work attempted'
                  : 'not reported'} />
          <Stat label="ETA (projected)"
                value={eta != null ? describeElapsed(Math.round(eta)) : '--'}
                hint={eta != null ? 'at the current rate' : 'needs a percentage'} />
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
            <Stat label="Data"
                  value={bytes(t.bytesMovedTotal)}
                  hint={`of ${bytes(t.expectedBytes)} discovered total`} />
          )}
        </Stack>

        {/* An indeterminate bar means "working, can't say how far". On a
            run that already ended it means nothing at all, so a finished
            job with no percentage gets no bar rather than a perpetual one. */}
        {typeof job.pct === 'number'
          ? <LinearProgress variant="determinate" value={job.pct} />
          : !job.done ? <LinearProgress /> : null}

        <Divider sx={{ my: 2 }} />
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

export default RunningJobDetail
