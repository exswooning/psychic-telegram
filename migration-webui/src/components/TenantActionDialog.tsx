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

export type Mode = 'wipe' | 'remove' | 'delete_users' | 'repair'

export const COPY: Record<Mode, { title: string; verb: string; warn: string }> = {
  wipe: {
    title: 'Wipe tenant data',
    verb: 'Wipe data',
    warn: 'Deletes the seeded Drive files, mail, calendar events, contacts '
        + 'and tasks. The Cloud project, the delegation grant and the saved '
        + 'configuration are kept, so the tenant stays ready to seed or '
        + 'migrate again.',
  },
  // The one action here that adds rather than removes. It sits with these
  // because it is per-tenant and needs the same admin password, not because
  // it is dangerous -- hence the plain colour on its button.
  repair: {
    title: 'Repair console setup',
    verb: 'Repair',
    warn: 'Re-pastes the delegation grant (picking up any scope added since '
        + 'this tenant was set up) and configures the Chat app. Both are '
        + 'console steps with no API, done once during setup and unreachable '
        + 'afterwards. Adds nothing and deletes nothing.',
  },
  delete_users: {
    title: 'Delete all users',
    verb: 'Delete users',
    warn: 'Deletes every migrated account in this tenant, not just its '
        + 'data, and invalidates the ledger that described them. A deleted '
        + 'Workspace address stays reserved for 20 days, so recreating one '
        + 'under the same name fails until it ages out.',
  },
  remove: {
    title: 'Remove tenant setup',
    verb: 'Remove setup',
    warn: 'Deletes the data AND the Cloud project, revokes the delegation '
        + 'grant, and forgets the configuration. Setting this tenant up '
        + 'again means a fresh sign-in and a fresh grant.',
  },
}

/** Only the teardown half signs in to Google. A wipe uses the service
 *  account already on file, so demanding a password for it would be asking
 *  for a credential nothing is going to use. */
export const needsPassword = (mode: Mode) =>
  mode === 'remove' || mode === 'repair'

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
