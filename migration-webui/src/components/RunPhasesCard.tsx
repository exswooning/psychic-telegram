/**
 * The latest migrate run's phases, in order, from its run record (`/api/v2/runs/{id}/phases`): which pass or
 * follow-on is done, running, stopped or failed, and where a restart picks up. A restart resumes at the first
 * phase not done, and only when the per-user ledger agrees nobody still needs the ones it skips.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Chip, Paper, Stack, Typography } from '@mui/material'
import { fetchRunPhases } from '@/api/controlPlane'
import type { PhaseState, RunPhases } from '@/api/controlPlane'

const COLOR: Record<PhaseState, 'default' | 'info' | 'success' | 'warning' | 'error'> = {
  PENDING: 'default', RUNNING: 'info', DONE: 'success', STOPPED: 'warning', FAILED: 'error',
}
const LABEL: Record<string, string> = {
  drive: 'Drive', after_drive: 'Shared drives + repair', gmail: 'Mail', repair: 'Repair', dms: 'Google import',
  tally: 'Tally',
}

export const RunPhasesCard: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [data, setData] = useState<RunPhases | null>(null)
  const load = useCallback(() => { fetchRunPhases(accountId).then(setData).catch(() => setData(null)) }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [load])
  const rec = data?.record
  if (!rec) return null
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="run-phases">
      <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>Run phases</Typography>
      <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" sx={{ mb: 1 }}>
        {rec.phases.map((p) => (
          <Chip key={p.seq} size="small" color={COLOR[p.state]} variant={p.state === 'PENDING' ? 'outlined' : 'filled'}
                label={`${LABEL[p.name] ?? p.name}: ${p.state.toLowerCase()}`} data-testid={`run-phase-${p.name}`} />
        ))}
      </Stack>
      <Typography variant="body2" color="text.secondary" data-testid="run-phases-resume">
        {rec.resumeFrom
          ? `A start now picks up at ${LABEL[rec.resumeFrom] ?? rec.resumeFrom}, if the ledger agrees nothing before it is owed.`
          : 'Every phase is done.'}
        {rec.resumes > 0 ? ` Resumed ${rec.resumes} time${rec.resumes === 1 ? '' : 's'}.` : ''}
      </Typography>
    </Paper>
  )
}

export default RunPhasesCard
