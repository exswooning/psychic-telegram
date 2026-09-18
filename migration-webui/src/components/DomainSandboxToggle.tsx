/**
 * Turn a domain's guard protection on or off.
 *
 * Every domain a setup wizard has configured is protected the moment it
 * is (domain_guard.py) -- the seeder and reset/wipe tooling refuse to
 * touch it. That is correct by default, and the ONLY way past it used to
 * be SSH and a CLI command, which cuts against operating this tool
 * through its own web UI. Now that every account needs an enrolled
 * authenticator to sign in at all, a superadmin-gated, typed-confirm,
 * fully-audited control here is not a weaker door -- it is the same door,
 * reached without a terminal.
 *
 * A switch, not a plain button: this is a durable state ("this domain
 * currently allows destructive tooling"), not a one-shot action, and a
 * switch is the control that says so at a glance without reading a log.
 * Turning it ON (declaring a sandbox) opens the same friction the actions
 * it unblocks already require -- type the domain back, give a reason.
 * Turning it OFF (restoring protection) is the safe direction and needs
 * neither; domain_guard.restore() takes no typed confirm for the same
 * reason.
 */
import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, Stack, Switch, TextField, Typography,
} from '@mui/material'
import {
  fetchDomainGuardStatus, revokeDomainGuard, restoreDomainGuard,
} from '@/api/controlPlane'

export const DomainSandboxToggle: React.FC<{ domain: string }> = ({ domain }) => {
  const [protectedState, setProtectedState] = useState<boolean | null>(null)
  const [revokedBy, setRevokedBy] = useState('')
  const [revokedReason, setRevokedReason] = useState('')
  const [open, setOpen] = useState(false)
  const [confirmTyped, setConfirmTyped] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const d = domain.trim().toLowerCase()

  const refresh = () => {
    if (!d) { setProtectedState(null); return }
    fetchDomainGuardStatus(d)
      .then((r) => {
        setProtectedState(r.protected)
        setRevokedBy(r.revokedBy || '')
        setRevokedReason(r.revokedReason || '')
      })
      .catch(() => setProtectedState(null))
  }

  useEffect(refresh, [d])

  // Unknown domain, or the status read failed -- render nothing rather
  // than a toggle whose state might be wrong. A switch that lies about
  // whether a tenant is protected is worse than no switch at all.
  if (!d || protectedState === null) return null

  const declare = async () => {
    setBusy(true); setErr('')
    try {
      const r = await revokeDomainGuard(d, reason.trim())
      if (!r.ok) throw new Error(r.detail || 'could not declare a sandbox')
      setOpen(false); setConfirmTyped(''); setReason('')
      refresh()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const restore = async () => {
    setBusy(true); setErr('')
    try {
      // No dialog: this is the direction that puts protection back on, and
      // domain_guard.restore() needs no typed confirm either.
      const r = await restoreDomainGuard(d, 'restored from the seed/reset page')
      if (!r.ok) throw new Error(r.detail || 'could not restore protection')
      refresh()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Box sx={{ mb: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <FormControlLabel
          sx={{ ml: 0 }}
          control={
            <Switch size="small" inputProps={{ 'data-testid': 'sandbox-toggle' } as never}
                    checked={!protectedState} disabled={busy}
                    onChange={(e) => (e.target.checked
                      ? setOpen(true)
                      : restore())} />
          }
          label={
            <Typography variant="body2">
              {protectedState
                ? `${d} is protected — seeding and reset are refused`
                : `${d} is a declared sandbox — protection is off`}
            </Typography>
          }
        />
      </Stack>
      {!protectedState && (revokedBy || revokedReason) && (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', ml: 4.5 }}
                    data-testid="sandbox-revoked-by">
          declared by {revokedBy || 'unknown'}{revokedReason ? ` — ${revokedReason}` : ''}
        </Typography>
      )}
      {err && <Alert severity="error" sx={{ mt: 1 }} data-testid="sandbox-error">{err}</Alert>}

      <Dialog open={open} onClose={() => (busy ? undefined : setOpen(false))}>
        <DialogTitle>Declare {d} a sandbox?</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            The seeder and the reset/wipe tooling will be allowed to empty or
            overwrite this tenant from here on. This is on the record — the
            reason below is read back at every server start until the switch
            is turned off again.
          </Typography>
          <TextField
            fullWidth size="small" label={`Type ${d} to confirm`}
            value={confirmTyped} onChange={(e) => setConfirmTyped(e.target.value)}
            inputProps={{ 'data-testid': 'sandbox-confirm-domain' }}
            sx={{ mb: 2 }}
          />
          <TextField
            fullWidth size="small" label="Reason" placeholder="e.g. rehearsal tenant, never holds real data"
            value={reason} onChange={(e) => setReason(e.target.value)}
            inputProps={{ 'data-testid': 'sandbox-reason' }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)} disabled={busy}>Cancel</Button>
          <Button color="warning" variant="contained" data-testid="sandbox-confirm"
                  disabled={busy || confirmTyped.trim().toLowerCase() !== d
                            || reason.trim().length < 3}
                  onClick={declare}>
            {busy ? 'Declaring…' : 'Declare sandbox'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

export default DomainSandboxToggle
