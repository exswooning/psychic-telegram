import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent,
  DialogTitle, Stack, TextField, Typography,
} from '@mui/material'
import { Warning as WarningIcon } from '@mui/icons-material'

/**
 * The single confirmation gate every write action in the control plane goes
 * through. One component rather than a per-action confirm, so a new dangerous
 * button cannot ship without the gate.
 *
 * Two independent controls, because they stop different mistakes:
 *
 *  - **Reason Code** (always). Free text, min 3 chars, written to
 *    operator_actions_log *before* the action runs. Stops unattributed
 *    history: three agents and several operators have touched this tenant
 *    pair, and "who ran the wipe, and why" has already been a real question.
 *  - **Typed confirmation** (destructive only). Echoing a literal word is
 *    the only control that reliably survives muscle memory -- an OK button
 *    gets clicked by reflex, `REVERT` has to be read.
 */
export interface ReasonCodeDialogProps {
  open: boolean
  title: string
  /** Plain-language description of the blast radius. Say what will change,
   *  not what the button is called. */
  description: React.ReactNode
  /** When set, the operator must type this exact string to enable Confirm. */
  confirmPhrase?: string
  destructive?: boolean
  busy?: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: (reason: string) => void
}

const ReasonCodeDialog: React.FC<ReasonCodeDialogProps> = ({
  open, title, description, confirmPhrase, destructive, busy, error,
  onCancel, onConfirm,
}) => {
  const [reason, setReason] = useState('')
  const [typed, setTyped] = useState('')

  // Never carry a previous action's reason into the next dialog -- that is
  // how an audit log fills up with copy-pasted text that explains nothing.
  useEffect(() => {
    if (open) { setReason(''); setTyped('') }
  }, [open])

  const reasonOk = reason.trim().length >= 3
  const phraseOk = !confirmPhrase || typed === confirmPhrase
  const canConfirm = reasonOk && phraseOk && !busy

  return (
    <Dialog open={open} onClose={busy ? undefined : onCancel} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        {destructive && <WarningIcon color="error" />}
        {title}
        {destructive && <Chip label="Destructive" color="error" size="small" />}
      </DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 0.5 }}>
          <Alert severity={destructive ? 'error' : 'info'} variant="outlined">
            {description}
          </Alert>

          <TextField
            label="Reason Code"
            placeholder="e.g. incident-42: reverting public shares from the link_flip run"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            multiline minRows={2} autoFocus fullWidth disabled={busy}
            error={reason.length > 0 && !reasonOk}
            helperText={
              reason.length > 0 && !reasonOk
                ? 'At least 3 characters.'
                : 'Recorded against your name in operator_actions_log before this runs.'
            }
          />

          {confirmPhrase && (
            <Box>
              <Typography variant="body2" sx={{ mb: 1 }}>
                Type <strong>{confirmPhrase}</strong> to confirm:
              </Typography>
              <TextField
                value={typed} onChange={(e) => setTyped(e.target.value)}
                fullWidth size="small" disabled={busy}
                error={typed.length > 0 && !phraseOk}
                inputProps={{ 'aria-label': `type ${confirmPhrase} to confirm` }}
              />
            </Box>
          )}

          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onCancel} disabled={busy}>Cancel</Button>
        <Button
          variant="contained"
          color={destructive ? 'error' : 'primary'}
          disabled={!canConfirm}
          onClick={() => onConfirm(reason.trim())}
        >
          {busy ? 'Working…' : 'Confirm'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}

export default ReasonCodeDialog
