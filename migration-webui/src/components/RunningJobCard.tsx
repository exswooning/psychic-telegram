/**
 * One running job, as a rectangle.
 *
 * "Running Now" used to be its own page, which meant an operator had to know
 * that a job they started on Jobs would be watched somewhere else. It is a
 * label on these cards now, not a destination -- same icon, same meaning,
 * one place.
 *
 * Every card answers the four things worth knowing at a glance: what kind of
 * job, which tenant it is happening to, how far along, and whether it is
 * still going.
 */
import React from 'react'
import {
  Box, Card, CardContent, Chip, LinearProgress, Stack, Tooltip, Typography,
} from '@mui/material'
import {
  Bolt as RunningIcon, Grass as SeedIcon, RocketLaunch as MigrateIcon,
  Language as DomainIcon, Settings as SetupIcon,
  DeleteSweep as ResetIcon, PersonAdd as ProvisionIcon,
} from '@mui/icons-material'
import type { JobKind, RunningJob } from '@/hooks/useRunningJobs'
import { describeElapsed } from '@/hooks/useRunningJobs'

const KIND: Record<JobKind, { label: string; icon: React.ReactElement;
                              color: 'success' | 'primary' | 'warning' | 'default' }> = {
  seed:      { label: 'Seed',      icon: <SeedIcon fontSize="small" />,      color: 'success' },
  migrate:   { label: 'Migrate',   icon: <MigrateIcon fontSize="small" />,   color: 'primary' },
  setup:     { label: 'Setup',     icon: <SetupIcon fontSize="small" />,     color: 'default' },
  reset:     { label: 'Reset',     icon: <ResetIcon fontSize="small" />,     color: 'warning' },
  provision: { label: 'Provision', icon: <ProvisionIcon fontSize="small" />, color: 'default' },
  other:     { label: 'Job',       icon: <RunningIcon fontSize="small" />,   color: 'default' },
}

export const RunningJobCard: React.FC<{
  job: RunningJob
  /** Rendered by the parent so this stays presentational. */
  action?: React.ReactNode
  /** Opens the full measurement view. The card is a glance; everything it
   *  cannot fit -- ETA, observed throughput, per-user detail -- lives one
   *  click away rather than nowhere. */
  onOpen?: () => void
}> = ({ job, action, onOpen }) => {
  const k = KIND[job.kind] ?? KIND.other
  const elapsed = job.elapsedSec ? describeElapsed(job.elapsedSec) : ''
  return (
    <Card variant="outlined"
          onClick={onOpen}
          sx={{ borderRadius: 2, minWidth: 300, flex: '1 1 340px',
                cursor: onOpen ? 'pointer' : 'default',
                '&:hover': onOpen ? { borderColor: 'primary.main' } : {} }}
          data-testid={`running-job-${job.kind}`}>
      <CardContent sx={{ pb: 1.5, '&:last-child': { pb: 1.5 } }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
          <Chip size="small" icon={k.icon} label={k.label} color={k.color}
                variant="outlined" />
          {/* The label, not a page. Same icon Running Now used. */}
          <Chip size="small" icon={<RunningIcon fontSize="small" />}
                label="Running now" color="info" />
          <Box sx={{ flexGrow: 1 }} />
          {/* Stop lives inside a clickable card, so its click must not
              also open the detail view. */}
          <Box onClick={(e) => e.stopPropagation()}>{action}</Box>
        </Stack>

        <Stack direction="row" alignItems="center" spacing={0.5} sx={{ mb: 0.5 }}>
          <DomainIcon fontSize="small" color="disabled" />
          <Typography variant="subtitle2" sx={{ fontWeight: 600 }} noWrap>
            {job.domain || job.label}
          </Typography>
        </Stack>

        <Tooltip title={job.detail}>
          <Typography variant="caption" color="text.secondary"
                      sx={{ display: 'block', mb: 1 }} noWrap>
            {job.detail}
          </Typography>
        </Tooltip>

        {/* A determinate bar when the job reports a percentage, an
            indeterminate one when it does not -- rather than a 0% that
            reads as "stuck". */}
        {typeof job.pct === 'number'
          ? <LinearProgress variant="determinate" value={job.pct} />
          : <LinearProgress />}

        <Stack direction="row" justifyContent="space-between" sx={{ mt: 0.5 }}>
          {/* Only when the detail line does not already lead with it. The
              webui job's detail is "32m 08s · <what it is doing>", and
              printing the duration again underneath it is the same fact
              twice on a card whose whole job is to be read at a glance. */}
          <Typography variant="caption" color="text.secondary">
            {elapsed && !job.detail.includes(elapsed) ? elapsed : ''}
          </Typography>
          {typeof job.pct === 'number' && (
            <Typography variant="caption" color="text.secondary"
                        sx={{ fontVariantNumeric: 'tabular-nums' }}>
              {job.pct}%
            </Typography>
          )}
        </Stack>
      </CardContent>
    </Card>
  )
}

export default RunningJobCard
