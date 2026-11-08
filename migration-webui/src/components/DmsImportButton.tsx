import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, TextField, Typography,
} from '@mui/material'
import { PlayArrow as RunIcon } from '@mui/icons-material'
import { runAction, fetchDmsStatus, ActionSpec } from '@/api/client'
import { useAccountPair } from '@/hooks/useAccountPair'

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
/* accountId: the account the page is VIEWING. Without it the import ran for the header's migration
   picker -- gorkhatweed (account 2) while the page showed account 3 (8 Nov 2026). Every other action on the
   page already passed it. */
/* action 'dms_reconcile': record which owed emails the DMS delivered, for named users only (no fresh import). */
const DmsImportButton: React.FC<{ spec: ActionSpec; accountId?: number | string; action?: 'dms_import' | 'dms_reconcile' | 'dms_wipe' | 'dms_wipe_dry' }> = ({ spec, accountId, action = 'dms_import' }) => {
  const record = action !== 'dms_import'
  const [open, setOpen] = useState(false)
  const pair = useAccountPair(accountId)
  const [typed, setTyped] = useState('')
  // A pilot: only these source users (comma-separated), optionally as a fresh import. Empty = the whole account.
  const [pilot, setPilot] = useState('')
  const [fresh, setFresh] = useState(false)
  const [running, setRunning] = useState(false)
  const [lines, setLines] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const sinceRef = useRef(0)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = useCallback(async () => {
    try {
      const job = await fetchDmsStatus(sinceRef.current, accountId)
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
  }, [accountId])

  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current)
  }, [])

  const start = async () => {
    setError(null)
    setLines([])
    sinceRef.current = 0
    const users = pilot.split(/[\s,;]+/).map((u) => u.trim()).filter(Boolean)
    if (record && !users.length) {
      setError('name the users to record')
      return
    }
    const r = users.length
      ? await runAction(action, spec.confirm, accountId, record ? { users } : { users, fresh })
      : await runAction(action, spec.confirm, accountId)
    if (!r.ok) {
      setError(r.error || 'could not start the DMS import')
      return
    }
    setOpen(false)
    setTyped('')
    setPilot('')
    setFresh(false)
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
        data-testid={`action-${action}`}
      >
        {running ? 'DMS job running…' : spec.label}
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
          {pair && <Typography variant="body2" sx={{ mb: 1, fontWeight: 700 }} data-testid="confirm-account">Runs for {pair}</Typography>}
          <Typography variant="body2" sx={{ mb: 2 }}>{spec.blurb}</Typography>
          <TextField fullWidth size="small" sx={{ mb: 1 }} value={pilot} onChange={(e) => setPilot(e.target.value)}
                     label={record ? 'These source users (up to 5)' : 'Pilot: only these source users (optional, up to 5)'}
                     placeholder={record ? 'ward7@s.example.com' : 'ward7@s.example.com — empty runs every user of this account'}
                     inputProps={{ 'data-testid': 'dms-pilot-users' }} />
          {!record && <FormControlLabel sx={{ mb: 1 }} label="Fresh import (replaces a finished import in the console; pilot users only)"
                            control={<Checkbox size="small" checked={fresh && !!pilot.trim()} disabled={!pilot.trim()}
                                               onChange={(e) => setFresh(e.target.checked)}
                                               inputProps={{ 'data-testid': 'dms-fresh' } as never} />} />}
          <Typography variant="body2" sx={{ mb: 1 }}>
            Type <strong>{spec.confirm}</strong> to run this.
          </Typography>
          <TextField
            fullWidth size="small" autoFocus value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={spec.confirm}
            inputProps={{ 'aria-label': `Type ${spec.confirm} to confirm` }}
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
