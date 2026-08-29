import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  TextField, Typography,
} from '@mui/material'
import { PlayArrow as RunIcon } from '@mui/icons-material'
import { runAction, fetchDmsStatus, ActionSpec } from '@/api/client'

/**
 * Starts Google's Data Migration Service mail import and watches its own
 * status feed.
 *
 * Deliberately NOT a JobRunner: this action runs on its own Job, parallel to
 * the engine migration, so it must poll /api/dms_status rather than /api/job
 * (which stays pointed at the migration). Google does the copying
 * server-side once Start import is pressed, so the real per-user progress
 * lives in the Admin console -- this feed just shows the browser driver
 * getting there and confirms it started.
 */
const DmsImportButton: React.FC<{ spec: ActionSpec }> = ({ spec }) => {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [running, setRunning] = useState(false)
  const [lines, setLines] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const sinceRef = useRef(0)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = useCallback(async () => {
    try {
      const job = await fetchDmsStatus(sinceRef.current)
      setRunning(job.running)
      if (job.lines.length) {
        sinceRef.current += job.lines.length
        setLines((prev) => [...prev, ...job.lines].slice(-40))
      }
      if (!job.running && pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    } catch {
      /* a dropped poll must not kill the stream; next tick retries */
    }
  }, [])

  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current)
  }, [])

  const start = async () => {
    setError(null)
    setLines([])
    sinceRef.current = 0
    const r = await runAction('dms_import', spec.confirm)
    if (!r.ok) {
      setError(r.error || 'could not start the DMS import')
      return
    }
    setOpen(false)
    setTyped('')
    setRunning(true)
    if (!pollRef.current) pollRef.current = setInterval(poll, 2500)
    poll()
  }

  return (
    <Box>
      <Button
        variant="outlined"
        color="primary"
        size="small"
        startIcon={<RunIcon />}
        disabled={running}
        onClick={() => setOpen(true)}
        data-testid="dms-import-btn"
      >
        {running ? 'DMS import running…' : spec.label}
      </Button>
      {running && (
        <Chip size="small" color="info" label="parallel to the migration"
              sx={{ ml: 1 }} />
      )}
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
            border: '1px solid', borderColor: 'divider', maxHeight: 220,
            overflow: 'auto', fontSize: 12, fontFamily: 'ui-monospace, monospace',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </Box>
      )}

      <Dialog open={open} onClose={() => setOpen(false)}>
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
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>Cancel</Button>
          <Button
            variant="contained"
            disabled={typed !== spec.confirm}
            onClick={start}
          >
            Run
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

export default DmsImportButton
