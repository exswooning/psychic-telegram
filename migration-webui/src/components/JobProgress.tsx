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
 * Polls continuously from mount, not only after `active` flips true --
 * webui.py's Job is a single global background process, so a page load
 * (or a second browser tab) while a run from earlier is still going must
 * discover and show it too, not stay blank until *this* component instance
 * is the one that pressed Start. `expectedName` is what makes that safe:
 * JOB.start()'s `name` argument ("seed", "reset target", "deploy") is
 * echoed back by /api/job, so this only ever renders output that actually
 * belongs to it, never a different tool's job that happens to be running.
 */
const JobProgress: React.FC<{
  active: boolean; expectedName: string
  onDone?: () => void; onRunningChange?: (running: boolean) => void
}> = ({ active, expectedName, onDone, onRunningChange }) => {
  const [visible, setVisible] = useState(false)
  const [running, setRunning] = useState(false)
  const [lines, setLines] = useState<string[]>([])
  const [rc, setRc] = useState<number | null>(null)
  const sinceRef = useRef(0)
  const wasRunning = useRef(false)
  const donePosted = useRef(false)

  const poll = useCallback(async () => {
    try {
      const job = await fetchJob(sinceRef.current)
      if (job.name !== expectedName) return
      setVisible(true)
      if (job.lines.length) {
        setLines((prev) => [...prev, ...job.lines])
        sinceRef.current = job.total
      }
      setRunning(job.running)
      onRunningChange?.(job.running)
      setRc(job.rc)
      if (job.running) { wasRunning.current = true; donePosted.current = false }
      if (!job.running && wasRunning.current && !donePosted.current) {
        donePosted.current = true
        onDone?.()
      }
    } catch {
      // A dropped poll must not kill the stream; the next tick retries.
    }
  }, [expectedName, onDone, onRunningChange])

  // Runs for the component's whole lifetime, independent of `active`, so an
  // already-running job of this name (started before this page loaded, or
  // from another tab) is discovered rather than requiring a fresh click.
  useEffect(() => {
    poll()
    const id = setInterval(poll, 1500)
    return () => clearInterval(id)
  }, [poll])

  // A fresh explicit start from *this* component: reset the transcript so
  // a previous run's output doesn't linger under the new one, and show the
  // panel immediately rather than waiting for the next poll tick.
  useEffect(() => {
    if (!active) return
    setLines([]); setRc(null); setRunning(true); setVisible(true)
    onRunningChange?.(true)
    sinceRef.current = 0
    wasRunning.current = true
    donePosted.current = false
  }, [active])   // eslint-disable-line react-hooks/exhaustive-deps

  if (!visible) return null

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
