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
import React from 'react'
import {
  Box, Chip, Dialog, DialogContent, DialogTitle, Divider, IconButton,
  LinearProgress, Stack, Typography,
} from '@mui/material'
import { Close as CloseIcon } from '@mui/icons-material'
import type { RunningJob } from '@/hooks/useRunningJobs'
import { describeElapsed } from '@/hooks/useRunningJobs'
import SeedRunDashboard from '@/components/SeedRunDashboard'

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

/** Straight-line projection from what has elapsed and what is done.
 *  Only honest while the rate holds, which is why it is labelled as a
 *  projection and rendered "--" rather than 0 when there is nothing to
 *  project from. */
export function projectedEta(pct: number | null | undefined,
                             elapsedSec: number | undefined): number | null {
  if (typeof pct !== 'number' || !elapsedSec || pct <= 0 || pct >= 100) return null
  return (elapsedSec / pct) * (100 - pct)
}

export const RunningJobDetail: React.FC<{
  job: RunningJob | null
  onClose: () => void
}> = ({ job, onClose }) => {
  if (!job) return null
  const eta = projectedEta(job.pct, job.elapsedSec)
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
          <Stat label="Progress"
                value={typeof job.pct === 'number' ? `${job.pct}%` : '--'}
                hint={typeof job.pct === 'number' ? undefined : 'not reported'} />
          <Stat label="ETA (projected)"
                value={eta != null ? describeElapsed(Math.round(eta)) : '--'}
                hint={eta != null ? 'at the current rate' : 'needs a percentage'} />
          <Stat label="State" value="running" />
        </Stack>

        {typeof job.pct === 'number'
          ? <LinearProgress variant="determinate" value={job.pct} />
          : <LinearProgress />}

        {/* A seed measures itself far better than a percentage can: observed
            writes per minute, per-user results, and an ETA from the run
            rather than from arithmetic. */}
        {job.lines && job.lines.length > 0 && (
          <>
            <Divider sx={{ my: 2 }} />
            <SeedRunDashboard lines={job.lines}
                              elapsedSec={job.elapsedSec ?? 0}
                              running />
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default RunningJobDetail
