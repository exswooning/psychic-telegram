import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  TextField, Typography,
} from '@mui/material'
import { PlayArrow as RunIcon, Stop as StopIcon } from '@mui/icons-material'
import { runAction, stopJob, fetchJob, ActionSpec } from '@/api/client'

/**
 * One button that runs a whitelisted ACTIONS entry and streams its output.
 *
 * Mirrors webui.py's own inline JS exactly: POST /api/run with the action
 * name (the server maps that to a fixed argv list -- nothing here can become
 * an arbitrary command), then poll /api/job for lines as they arrive. A
 * destructive action shows a confirm dialog asking for the exact phrase the
 * server itself checks, so a mis-click still cannot fire it -- the check is
 * real on the server; this dialog only saves a wasted round trip.
 */
const JobRunner: React.FC<{ name: string; spec: ActionSpec; onDone?: () => void }> = ({
  name, spec, onDone,
}) => {
  const [running, setRunning] = useState(false)
  const [lines, setLines] = useState<string[]>([])
  const [rc, setRc] = useState<number | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [error, setError] = useState<string | null>(null)
  const sinceRef = useRef(0)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = useCallback(async () => {
    try {
      const job = await fetchJob(sinceRef.current)
      if (job.lines.length) {
        setLines((prev) => [...prev, ...job.lines])
        sinceRef.current = job.total
      }
      setRunning(job.running)
      setRc(job.rc)
      if (!job.running && pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
        onDone?.()
      }
    } catch {
      // A dropped poll must not kill the stream; the next tick retries.
    }
  }, [onDone])

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const start = useCallback(async (confirm?: string) => {
    setError(null)
    setLines([])
    sinceRef.current = 0
    const res = await runAction(name, confirm)
    if (!res.ok) {
      setError(res.error || 'could not start')
      return
    }
    setRunning(true)
    pollRef.current = setInterval(poll, 1000)
    poll()
  }, [name, poll])

  const handleClick = () => {
    if (spec.destructive) {
      setTyped('')
      setConfirmOpen(true)
    } else {
      start()
    }
  }

  const handleConfirm = () => {
    setConfirmOpen(false)
    start(typed)
  }

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Button
          variant={spec.destructive ? 'outlined' : 'contained'}
          color={spec.destructive ? 'error' : 'primary'}
          size="small"
          startIcon={running ? <StopIcon /> : <RunIcon />}
          onClick={running ? () => stopJob() : handleClick}
          // Keyed by action name so a harness can address any action without
          // matching on button TEXT, which is a label people reword.
          data-testid={`action-${name}`}
        >
          {running ? 'Stop' : spec.label}
        </Button>
        {rc !== null && !running && (
          <Chip
            size="small"
            label={rc === 0 ? 'exit 0' : `exit ${rc}`}
            color={rc === 0 ? 'success' : 'error'}
            data-testid={`action-exit-${name}`}
          />
        )}
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
        {spec.blurb}
      </Typography>
      {error && (
        <Typography variant="caption" color="error" sx={{ display: 'block', mt: 0.5 }}>
          {error}
        </Typography>
      )}
      {lines.length > 0 && (
        <Box
          component="pre"
          sx={{
            mt: 1, p: 1.5, bgcolor: 'background.default', borderRadius: 1,
            border: '1px solid', borderColor: 'divider', maxHeight: 260,
            overflow: 'auto', fontSize: 12, fontFamily: 'ui-monospace, monospace',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </Box>
      )}

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <DialogTitle>Confirm: {spec.label}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 2 }}>{spec.blurb}</Typography>
          <Typography variant="body2" sx={{ mb: 1 }}>
            Type <strong>{spec.confirm}</strong> to run this.
          </Typography>
          <TextField
            fullWidth size="small" autoFocus value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={spec.confirm}
            inputProps={{ 'data-testid': `action-confirm-input-${name}` }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button
            color="error" variant="contained"
            disabled={typed !== spec.confirm}
            onClick={handleConfirm}
            data-testid={`action-confirm-${name}`}
          >
            Run
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

export default JobRunner
