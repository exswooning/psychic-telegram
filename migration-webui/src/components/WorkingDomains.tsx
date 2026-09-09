/**
 * The domains this deployment is actually set up against, and what can be
 * done to them.
 *
 * Two very different actions live here, and the distinction is the whole
 * point of showing them together:
 *
 *   Wipe      empties the tenant's seeded data and leaves it usable. The
 *             next seed or migration runs without any further setup.
 *   Remove    also deletes the Cloud project, revokes the delegation grant
 *             and forgets the configuration. Setting the tenant up again is
 *             a fresh sign-in and a fresh grant.
 *
 * Both are gated on typing the domain rather than a generic word. "DELETE"
 * proves someone read a dialog; the domain proves they know which of two
 * configured tenants they are pointed at, which is the mistake worth
 * catching when both are one click apart on the same card.
 */
import React, { useState } from 'react'
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Dialog,
  DialogActions, DialogContent, DialogTitle, Divider, LinearProgress, Stack,
  TextField, Typography,
} from '@mui/material'
import { useRunningJobs } from '@/hooks/useRunningJobs'
import {
  DeleteForever as RemoveIcon, DeleteSweep as WipeIcon,
  PersonRemove as UsersIcon, Build as RepairIcon,
  Language as DomainIcon,
} from '@mui/icons-material'

export interface ConfiguredTenant {
  side: 'source' | 'target'
  domain: string
  adminEmail?: string
  project?: string
  clientId?: string
}

type Mode = 'wipe' | 'remove' | 'delete_users' | 'repair'

const COPY: Record<Mode, { title: string; verb: string; warn: string }> = {
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

export const WorkingDomains: React.FC<{
  tenants: ConfiguredTenant[]
  onAct: (t: ConfiguredTenant, mode: Mode, password: string) => Promise<void>
}> = ({ tenants, onAct }) => {
  // Every action on this card starts a job somewhere else and says nothing
  // more about it. A repair ran for twelve seconds with the header pill
  // counting up and this card showing four idle buttons, which reads as
  // "nothing happened, click it again" -- on buttons that wipe tenants.
  //
  // Same hook the Jobs page uses, so this is not a second poller: it
  // already reports webui's per-account Job, which is what all four of
  // these launch.
  const { jobs } = useRunningJobs()
  const [target, setTarget] = useState<{ t: ConfiguredTenant; mode: Mode } | null>(null)
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const configured = tenants.filter((t) => t.domain)
  const ask = (t: ConfiguredTenant, mode: Mode) => {
    setTarget({ t, mode }); setTyped(''); setPassword(''); setError('')
  }
  const matches = !!target &&
    typed.trim().toLowerCase() === target.t.domain.toLowerCase()
  // Only the teardown half signs in to Google. A wipe uses the service
  // account that is already on file, so demanding a password for it would
  // be asking for a credential nothing is going to use.
  const needsPassword = target?.mode === 'remove'
                     || target?.mode === 'repair'
  const copy = target ? COPY[target.mode] : null

  return (
    <Card variant="outlined" sx={{ borderRadius: 2, mb: 3 }}
          data-testid="working-domains">
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>
          Working domains
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          The tenants this deployment is set up against. The migration ledger
          is never touched by either action — it records what was migrated,
          and outlives the tenant it happened to.
        </Typography>

        {!configured.length && (
          <Alert severity="info">
            No tenant is set up yet. Run the Setup Wizard first.
          </Alert>
        )}

        <Stack divider={<Divider flexItem />} spacing={0}>
          {configured.map((t) => (
            <Box key={t.side} sx={{ py: 1.5 }} data-testid={`domain-${t.side}`}>
              <Stack direction="row" alignItems="center" spacing={1}
                     flexWrap="wrap">
                <DomainIcon fontSize="small" color="disabled" />
                <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                  {t.domain}
                </Typography>
                <Chip size="small" variant="outlined" label={t.side} />
                <Box sx={{ flexGrow: 1 }} />
                <Button size="small" color="warning" variant="outlined"
                        startIcon={<WipeIcon />}
                        data-testid={`wipe-${t.side}`}
                        onClick={() => ask(t, 'wipe')}>
                  Wipe data
                </Button>
                <Button size="small" variant="outlined"
                        startIcon={<RepairIcon />}
                        data-testid={`repair-${t.side}`}
                        onClick={() => ask(t, 'repair')}>
                  Repair console
                </Button>
                <Button size="small" color="error" variant="outlined"
                        startIcon={<UsersIcon />}
                        data-testid={`delete-users-${t.side}`}
                        onClick={() => ask(t, 'delete_users')}>
                  Delete users
                </Button>
                <Button size="small" color="error" variant="outlined"
                        startIcon={<RemoveIcon />}
                        data-testid={`remove-${t.side}`}
                        onClick={() => ask(t, 'remove')}>
                  Remove setup
                </Button>
              </Stack>
              {(() => {
                // Matched on domain, which is what the hook labels a job
                // with -- not on which button was pressed, because a job
                // started from the Jobs page acts on this tenant too and a
                // reader watching this card should see that.
                const live = jobs.find((j) => j.domain && j.domain === t.domain)
                if (!live) return null
                return (
                  <Box sx={{ mt: 1, mb: 0.5 }} data-testid={`running-${t.side}`}>
                    <Stack direction="row" spacing={1} alignItems="center">
                      <CircularProgress size={13} thickness={6} />
                      <Typography variant="caption" sx={{ fontWeight: 600 }}>
                        {live.label}
                      </Typography>
                      <Typography variant="caption" color="text.secondary"
                                  sx={{ flexGrow: 1, overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap' }}>
                        {live.detail}
                      </Typography>
                    </Stack>
                    {/* Determinate when the job counts itself, indeterminate
                        when it does not -- a full bar on a job with no
                        percentage is a worse lie than no bar. */}
                    {typeof live.pct === 'number'
                      ? <LinearProgress variant="determinate" value={live.pct}
                                        sx={{ mt: 0.5, height: 5, borderRadius: 3 }} />
                      : <LinearProgress sx={{ mt: 0.5, height: 5, borderRadius: 3 }} />}
                  </Box>
                )
              })()}
              <Typography variant="caption" color="text.secondary">
                {t.adminEmail || 'no admin on file'}
                {t.clientId ? ` · client ${t.clientId}` : ''}
                {t.project ? ` · project ${t.project}` : ''}
              </Typography>
            </Box>
          ))}
        </Stack>
      </CardContent>

      <Dialog open={!!target} onClose={() => !busy && setTarget(null)}
              maxWidth="sm" fullWidth>
        <DialogTitle>{copy?.title} — {target?.t.domain}</DialogTitle>
        <DialogContent>
          <Alert severity={target?.mode === 'remove' ? 'error' : 'warning'}
                 sx={{ mb: 2 }}>
            {copy?.warn}
          </Alert>
          <TextField
            fullWidth size="small" sx={{ mb: 2 }}
            label={`Type ${target?.t.domain} to confirm`}
            value={typed} onChange={(e) => setTyped(e.target.value)}
            inputProps={{ 'data-testid': 'confirm-domain' }}
          />
          {needsPassword && (
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
          <Button onClick={() => setTarget(null)} disabled={busy}>Cancel</Button>
          <Button variant="contained" data-testid="confirm-act"
                  color={target?.mode === 'remove' ? 'error' : 'warning'}
                  disabled={!matches || (needsPassword && !password) || busy}
                  onClick={async () => {
                    if (!target) return
                    setBusy(true); setError('')
                    try {
                      await onAct(target.t, target.mode, password)
                      setTarget(null)
                    } catch (e) {
                      setError(String(e))
                    } finally {
                      setBusy(false)
                    }
                  }}>
            {busy ? 'Working…' : copy?.verb}
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  )
}

export default WorkingDomains
