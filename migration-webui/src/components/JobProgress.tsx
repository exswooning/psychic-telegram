import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Box, Button, Chip, LinearProgress, Typography } from '@mui/material'
import { Stop as StopIcon } from '@mui/icons-material'
import { fetchJob, stopJob } from '@/api/client'

/**
 * Live output + progress for the one background job webui.py can run at a
 * time, for the endpoints that start it directly (POST /api/seed,
 * /api/reset_target, /api/deploy) rather than through the whitelisted
 * ACTIONS map JobRunner drives. Same backend Job object, same /api/job
 * polling JobRunner already does -- this is that same behaviour, usable
 * where there is no ActionSpec to hand it.
 *
 * Before this, "Start seeding" showed one static sentence
 * ("seeding started -- watch the Activity Feed") and nothing else ever
 * appeared here again: no progress, no output, no indication of a failure
 * that happens before anything reaches the ledger (an unauthorized_client
 * scope error, for instance, which never writes a single audit_log row --
 * so the Activity Feed staying empty was correct, just unexplained).
 *
 * `expectedName` guards against a stale poll from a previous run being
 * mistaken for the one this instance just started -- JOB.start()'s `name`
 * argument ("seed", "reset target", "deploy") is echoed back by /api/job,
 * so a leftover interval from an earlier mount cannot render as if it were
 * this action's live output.
 *
 * `active` only ever gates *starting* a fresh poll cycle (the caller flips
 * it true right after a successful start); once a job finishes this keeps
 * rendering its final output and exit code rather than disappearing the
 * instant it completes -- disable the caller's own start button via
 * `onRunningChange` instead of by toggling `active` off.
 */
const JobProgress: React.FC<{
  active: boolean; expectedName: string
  onDone?: () => void; onRunningChange?: (running: boolean) => void
}> = ({ active, expectedName, onDone, onRunningChange }) => {
  const [running, setRunning] = useState(false)
  const [lines, setLines] = useState<string[]>([])
  const [rc, setRc] = useState<number | null>(null)
  const sinceRef = useRef(0)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const wasRunning = useRef(false)

  const poll = useCallback(async () => {
    try {
      const job = await fetchJob(sinceRef.current)
      if (job.name !== expectedName) return
      if (job.lines.length) {
        setLines((prev) => [...prev, ...job.lines])
        sinceRef.current = job.total
      }
      setRunning(job.running)
      onRunningChange?.(job.running)
      setRc(job.rc)
      if (job.running) wasRunning.current = true
      if (!job.running && wasRunning.current && pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
        onDone?.()
      }
    } catch {
      // A dropped poll must not kill the stream; the next tick retries.
    }
  }, [expectedName, onDone, onRunningChange])

  useEffect(() => {
    if (!active) return
    setLines([]); setRc(null); setRunning(true)
    onRunningChange?.(true)
    sinceRef.current = 0
    wasRunning.current = false
    poll()
    pollRef.current = setInterval(poll, 1000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  if (!active) return null

  return (
    <Box sx={{ mt: 2 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
        {running ? (
          <>
            <Box sx={{ flexGrow: 1 }}>
              <LinearProgress />
            </Box>
            <Button size="small" startIcon={<StopIcon />} onClick={() => stopJob()}>
              Stop
            </Button>
          </>
        ) : rc !== null ? (
          <Chip
            size="small"
            label={rc === 0 ? 'finished -- exit 0' : `failed -- exit ${rc}`}
            color={rc === 0 ? 'success' : 'error'}
          />
        ) : null}
      </Box>
      {lines.length > 0 && (
        <Box
          component="pre"
          sx={{
            p: 1.5, bgcolor: 'background.default', borderRadius: 1,
            border: '1px solid', borderColor: 'divider', maxHeight: 320,
            overflow: 'auto', fontSize: 12, fontFamily: 'ui-monospace, monospace',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </Box>
      )}
      {!running && rc === null && (
        <Typography variant="caption" color="text.secondary">
          Waiting for output…
        </Typography>
      )}
    </Box>
  )
}

export default JobProgress
