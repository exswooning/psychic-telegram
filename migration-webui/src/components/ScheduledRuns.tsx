import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Stack, Typography } from '@mui/material'
import { cancelScheduledRun, fetchScheduledRuns, type ScheduledRun } from '@/api/controlPlane'

/** Runs set for an hour: what is waiting, and what happened to the last few. Shows nothing
 *  until one exists. */
const ScheduledRuns: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [runs, setRuns] = useState<ScheduledRun[]>([])
  const load = useCallback(() => {
    Promise.resolve().then(() => fetchScheduledRuns(accountId))
      .then((r) => setRuns(r?.runs ?? [])).catch(() => {})
  }, [accountId])
  useEffect(() => { load() }, [load])
  const shown = runs.filter((r) => r.status === 'PENDING' || r.status === 'STARTING' || runs.indexOf(r) < 3)
  if (!shown.length) return null
  return (
    <Box sx={{ mb: 2 }} data-testid="scheduled-runs">
      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 0.5 }}>Scheduled runs</Typography>
      <Stack spacing={0.5}>
        {shown.map((r) => (
          <Alert key={r.id} severity={r.status === 'FAILED' || r.status === 'MISSED' ? 'warning' : 'info'}
                 icon={false} data-testid={`scheduled-${r.id}`}
                 action={r.status === 'PENDING' ? (
                   <Button color="inherit" size="small" onClick={() => cancelScheduledRun(r.id, accountId).then(load).catch(() => {})}>
                     Cancel
                   </Button>) : undefined}>
            <strong>{new Date(r.runAt).toLocaleString()}</strong> — {r.status.toLowerCase()}
            {r.detail ? `: ${r.detail}` : ''}
          </Alert>
        ))}
      </Stack>
    </Box>
  )
}

export default ScheduledRuns
