/**
 * The typed-domain gate in front of anything that destroys a tenant.
 *
 * Lifted out of WorkingDomains so the Jobs page's per-tenant cards can put
 * the same two actions where an operator is already looking at that
 * tenant's history -- with the same wording, the same gate and the same
 * copy explaining what survives. A second confirm pattern for the same
 * destructive action is how one of them ends up weaker than the other.
 *
 * Typing the domain rather than a generic word is the whole point: "DELETE"
 * proves someone read a dialog, the domain proves they know which of two
 * configured tenants they are pointed at -- the mistake worth catching when
 * both are one click apart.
 */
import React, { useEffect, useState } from 'react'
import {
  Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  TextField,
} from '@mui/material'

import { Mode, COPY, needsPassword } from './TenantActionDialog.utils'

export const TenantActionDialog: React.FC<{
  /** null closes it. */
  target: { domain: string; mode: Mode } | null
  onCancel: () => void
  onConfirm: (password: string) => Promise<void>
}> = ({ target, onCancel, onConfirm }) => {
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // Cleared when the dialog is pointed at something new. Without this a
  // domain typed for one tenant stays satisfied for the next one opened,
  // which defeats the entire gate.
  useEffect(() => {
    setTyped(''); setPassword(''); setError('')
  }, [target?.domain, target?.mode])

  if (!target) return null
  const copy = COPY[target.mode]
  const matches = typed.trim().toLowerCase() === target.domain.toLowerCase()
  const wantsPassword = needsPassword(target.mode)
  const severe = target.mode === 'remove'

  return (
    <Dialog open onClose={() => !busy && onCancel()} maxWidth="sm" fullWidth>
      <DialogTitle>{copy.title} — {target.domain}</DialogTitle>
      <DialogContent>
        <Alert severity={severe ? 'error' : 'warning'} sx={{ mb: 2 }}>
          {copy.warn}
        </Alert>
        <TextField
          fullWidth size="small" sx={{ mb: 2 }}
          label={`Type ${target.domain} to confirm`}
          value={typed} onChange={(e) => setTyped(e.target.value)}
          inputProps={{ 'data-testid': 'confirm-domain' }}
        />
        {wantsPassword && (
          <TextField
            fullWidth size="small" type="password"
            label="Super admin password"
            helperText="Deleting the project and revoking the grant both
                        sign in to Google"
            value={password} onChange={(e) => setPassword(e.target.value)}
            inputProps={{ 'data-testid': 'admin-password' }}
          />
        )}
        {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onCancel} disabled={busy}>Cancel</Button>
        <Button variant="contained" data-testid="confirm-act"
                color={severe ? 'error' : 'warning'}
                disabled={!matches || (wantsPassword && !password) || busy}
                onClick={async () => {
                  setBusy(true); setError('')
                  try {
                    await onConfirm(password)
                    onCancel()
                  } catch (e) {
                    setError(String(e))
                  } finally {
                    setBusy(false)
                  }
                }}>
          {busy ? 'Working…' : copy.verb}
        </Button>
      </DialogActions>
    </Dialog>
  )
}

export default TenantActionDialog
