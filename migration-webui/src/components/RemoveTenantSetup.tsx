/**
 * Wipe a configured tenant's data and remove the setup itself.
 *
 * The most destructive control in the product: it undoes a whole tenant
 * setup -- the data, the Cloud project, the delegation grant and the
 * configuration -- in one run.
 *
 * The order is not adjustable and is the reason this is one button rather
 * than three: the data wipe needs the credential the teardown destroys, so
 * doing them separately in the wrong order leaves a tenant full of data and
 * nothing left that can reach it.
 *
 * Gated on typing the domain, not a generic word. "TRASH" or "DELETE"
 * confirms that someone read a dialog; the domain confirms they know which
 * tenant they are pointed at, which is the mistake actually worth catching
 * when two are configured and both are one click apart.
 */
import React, { useState } from 'react'
import {
  Alert, Box, Button, Card, CardContent, Dialog, DialogActions,
  DialogContent, DialogTitle, Stack, TextField, Typography,
} from '@mui/material'
import { DeleteForever as RemoveIcon } from '@mui/icons-material'

export interface ConfiguredTenant {
  side: 'source' | 'target'
  domain: string
  adminEmail?: string
  project?: string
  clientId?: string
}

export const RemoveTenantSetup: React.FC<{
  tenants: ConfiguredTenant[]
  onRemove: (t: ConfiguredTenant, password: string) => Promise<void>
}> = ({ tenants, onRemove }) => {
  const [target, setTarget] = useState<ConfiguredTenant | null>(null)
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const configured = tenants.filter((t) => t.domain)
  if (!configured.length) return null

  const matches = !!target && typed.trim().toLowerCase() === target.domain.toLowerCase()

  return (
    <Card variant="outlined"
          sx={{ borderRadius: 2, borderColor: 'error.light', mb: 3 }}
          data-testid="remove-tenant-setup">
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>
          Remove a tenant setup
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Wipes the tenant&apos;s seeded data, deletes its Cloud project,
          revokes the delegation grant, and forgets the configuration. The
          migration ledger is kept — it records what was migrated, and
          outlives the tenant it happened to.
        </Typography>
        <Stack direction="row" spacing={1} flexWrap="wrap">
          {configured.map((t) => (
            <Button key={t.side} size="small" color="error" variant="outlined"
                    startIcon={<RemoveIcon />}
                    data-testid={`remove-${t.side}`}
                    onClick={() => { setTarget(t); setTyped(''); setPassword(''); setError('') }}>
              {t.side}: {t.domain}
            </Button>
          ))}
        </Stack>
      </CardContent>

      <Dialog open={!!target} onClose={() => !busy && setTarget(null)}
              maxWidth="sm" fullWidth>
        <DialogTitle>Remove {target?.side} setup</DialogTitle>
        <DialogContent>
          <Alert severity="error" sx={{ mb: 2 }}>
            This deletes <strong>{target?.domain}</strong>&apos;s seeded data,
            its Cloud project{target?.project ? ` (${target.project})` : ''},
            its delegation grant and its saved configuration. The data wipe
            runs first, because it needs the credential the teardown removes.
          </Alert>
          <TextField
            fullWidth size="small" sx={{ mb: 2 }}
            label={`Type ${target?.domain} to confirm`}
            value={typed} onChange={(e) => setTyped(e.target.value)}
            inputProps={{ 'data-testid': 'confirm-domain' }}
          />
          <TextField
            fullWidth size="small" type="password"
            label="Super admin password"
            helperText="Needed to sign in to the Cloud console and Admin console"
            value={password} onChange={(e) => setPassword(e.target.value)}
            inputProps={{ 'data-testid': 'admin-password' }}
          />
          {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setTarget(null)} disabled={busy}>Cancel</Button>
          <Button color="error" variant="contained"
                  data-testid="confirm-remove"
                  disabled={!matches || !password || busy}
                  onClick={async () => {
                    if (!target) return
                    setBusy(true); setError('')
                    try {
                      await onRemove(target, password)
                      setTarget(null)
                    } catch (e) {
                      setError(String(e))
                    } finally {
                      setBusy(false)
                    }
                  }}>
            {busy ? 'Removing…' : 'Remove setup'}
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  )
}

export default RemoveTenantSetup
