import React, { useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, TextField, Button, Alert,
} from '@mui/material'
import { Build as MaintenanceIcon } from '@mui/icons-material'
import { fetchActions, fetchConfig, runResetDriveLedger, runWipeTarget, runWipeSource, ActionSpec } from '@/api/client'
import JobRunner from '@/components/JobRunner'

/**
 * Operator/superadmin-only ops panel -- ledger repair actions that only
 * make sense between runs, not something a SaaS client self-serving one
 * tenant should ever need. Replaces the legacy dashboard's Maintenance
 * tab, minus the one button that tab shipped broken: "Reset Drive Ledger"
 * called an ACTIONS key that never existed. reset_drive_ledger.py itself
 * predates this page (built earlier this session); this is its first
 * real UI.
 */
import { MAINTENANCE_KEYS, unclaimed } from '@/actionHomes'

const Maintenance: React.FC = () => {
  const [actions, setActions] = useState<Record<string, ActionSpec>>({})

  useEffect(() => { fetchActions().then(setActions) }, [])

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <MaintenanceIcon color="action" />
        <Typography variant="h4" sx={{ fontWeight: 700 }}>Maintenance</Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Ledger repair actions -- for fixing state between runs, not part of a normal migration.
      </Typography>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent>
          <Stack spacing={3}>
            {MAINTENANCE_KEYS.map((key) => (
              actions[key] && <JobRunner key={key} name={key} spec={actions[key]} />
            ))}
            {Object.keys(actions).length === 0 && (
              <Typography variant="body2" color="text.secondary">Loading…</Typography>
            )}
          </Stack>
        </CardContent>
      </Card>

      {/* Anything the backend offers that no page has spoken for. Without
          this, adding an action to ACTIONS put it on screen nowhere -- 16 of
          43 were in that state when this was written. A slightly odd home
          beats none. */}
      {unclaimed(Object.keys(actions)).length > 0 && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
          <CardContent>
            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
              Everything else
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Actions with no dedicated page. They appear here automatically so
              nothing the server can run is invisible.
            </Typography>
            <Stack spacing={3}>
              {unclaimed(Object.keys(actions)).map((key) => (
                <JobRunner key={key} name={key} spec={actions[key]} />
              ))}
            </Stack>
          </CardContent>
        </Card>
      )}

      <ResetDriveLedgerCard />
      <WipeTargetCard />
      <WipeSourceCard />
    </Box>
  )
}

/**
 * Not a plain ACTIONS entry -- needs the source domain typed back to
 * confirm, the same pattern reset_target.py's own form uses (see
 * SeedWizard.tsx's ResetTargetStep), because reset_drive_ledger.py always
 * operates on source_email keys and a fixed confirm phrase would not
 * catch pointing this at the wrong tenant.
 */
const ResetDriveLedgerCard: React.FC = () => {
  const [domain, setDomain] = useState('')
  const [confirmDomain, setConfirmDomain] = useState('')
  const [services, setServices] = useState('')
  const [account, setAccount] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [ok, setOk] = useState(false)

  useEffect(() => {
    fetchConfig().then((c) => setDomain(c.config.source_domain || ''))
  }, [])

  const run = async () => {
    setErr(null); setOk(false)
    const r = await runResetDriveLedger(confirmDomain, services || undefined,
                                        account || undefined)
    if (r.ok) setOk(true)
    else setErr(r.error || 'could not start')
  }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>Reset Drive ledger</Typography>
        <Alert severity="warning" sx={{ mb: 2 }}>
          Clears Drive resume state so the next migrate/delta pass re-copies
          files reset_target.py deleted. Always operates on the SOURCE
          tenant's identities, regardless of which tenant's files were
          actually wiped -- type the source domain (
          <strong>{domain || 'not set'}</strong>) to confirm.
        </Alert>
        <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
          <TextField
            size="small" label="Type the source domain to confirm"
            value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
            sx={{ width: 280 }}
          />
          <TextField
            size="small" label="Services (optional, e.g. drive)"
            value={services} onChange={(e) => setServices(e.target.value)}
            sx={{ width: 220 }}
          />
          <TextField
            size="small" label="Account id (blank = mine)"
            value={account} onChange={(e) => setAccount(e.target.value)}
            sx={{ width: 200 }}
          />
          <Button variant="outlined" color="warning" disabled={!confirmDomain} onClick={run}>
            Reset Drive ledger
          </Button>
        </Stack>
        {ok && <Alert severity="success" sx={{ mt: 2 }}>Started -- check Mission Control for output.</Alert>}
        {err && <Alert severity="error" sx={{ mt: 2 }}>{err}</Alert>}
      </CardContent>
    </Card>
  )
}

/**
 * reset_target empties the seeded data; the users provisioning created stay.
 * A rehearsal on top of them is not a rehearsal -- provisioning skips users
 * that already exist, so the copy lands on the previous one and the fidelity
 * check compares the tenant against itself.
 */
const WipeTargetCard: React.FC = () => {
  const [domain, setDomain] = useState('')
  const [confirmDomain, setConfirmDomain] = useState('')
  const [account, setAccount] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [ok, setOk] = useState(false)

  useEffect(() => {
    fetchConfig().then((c) => setDomain(c.config.target_domain || ''))
  }, [])

  const run = async () => {
    setErr(null); setOk(false)
    const r = await runWipeTarget(confirmDomain, account || undefined)
    if (r.ok) setOk(true)
    else setErr(r.error || 'could not start')
  }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mt: 3 }}>
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>Wipe target accounts</Typography>
        <Alert severity="error" sx={{ mb: 2 }}>
          Deletes every provisioned user on the TARGET tenant (
          <strong>{account ? `whichever target account ${account} has configured`
                           : (domain || 'not set')}</strong>) and invalidates
          the ledger describing them. The admin driving it is never deleted. Deleted
          Workspace users are restorable for 20 days. Type the target domain
          to confirm.
        </Alert>
        <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
          <TextField
            size="small" label="Type the target domain to confirm"
            inputProps={{ 'data-testid': 'wipe-target-domain' }}
            value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
            sx={{ width: 280 }}
          />
          <TextField
            size="small" label="Account id (blank = mine)"
            inputProps={{ 'data-testid': 'wipe-target-account' }}
            value={account} onChange={(e) => setAccount(e.target.value)}
            sx={{ width: 200 }}
          />
          <Button variant="outlined" color="error" disabled={!confirmDomain} onClick={run}>
            Wipe target accounts
          </Button>
        </Stack>
        {ok && <Alert severity="success" sx={{ mt: 2 }}>Started -- check Mission Control for output.</Alert>}
        {err && <Alert severity="error" sx={{ mt: 2 }}>{err}</Alert>}
      </CardContent>
    </Card>
  )
}

/**
 * Emptying the target is routine between rehearsals. Emptying the SOURCE
 * destroys what the migration exists to move, so it is a separate card with
 * its own typed domain -- not a checkbox on the one above, where muscle
 * memory would eventually fire it.
 */
const WipeSourceCard: React.FC = () => {
  const [domain, setDomain] = useState('')
  const [confirmDomain, setConfirmDomain] = useState('')
  const [account, setAccount] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [ok, setOk] = useState(false)

  useEffect(() => {
    fetchConfig().then((c) => setDomain(c.config.source_domain || ''))
  }, [])

  const run = async () => {
    setErr(null); setOk(false)
    const r = await runWipeSource(confirmDomain, account || undefined)
    if (r.ok) setOk(true)
    else setErr(r.error || 'could not start')
  }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'error.main', mt: 3 }}>
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>
          Wipe SOURCE accounts
        </Typography>
        <Alert severity="error" sx={{ mb: 2 }}>
          Deletes every user on the SOURCE tenant (
          <strong>{account ? `whichever source account ${account} has configured`
                           : (domain || 'not set')}</strong>) — the corpus this
          migration exists to move. Only correct when reseeding under different
          usernames, where the old accounts must be gone rather than emptied.
          Deleted users keep consuming the domain user limit for 20 days, so
          this borrows against the next three weeks of capacity rather than
          returning any. Type the source domain to confirm.
        </Alert>
        <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
          <TextField
            size="small" label="Type the source domain to confirm"
            inputProps={{ 'data-testid': 'wipe-source-domain' }}
            value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
            sx={{ width: 280 }}
          />
          <TextField
            size="small" label="Account id (blank = mine)"
            inputProps={{ 'data-testid': 'wipe-source-account' }}
            value={account} onChange={(e) => setAccount(e.target.value)}
            sx={{ width: 200 }}
          />
          <Button variant="contained" color="error" disabled={!confirmDomain} onClick={run}>
            Wipe source accounts
          </Button>
        </Stack>
        {ok && <Alert severity="success" sx={{ mt: 2 }}>Started — check Running Now.</Alert>}
        {err && <Alert severity="error" sx={{ mt: 2 }}>{err}</Alert>}
      </CardContent>
    </Card>
  )
}

export default Maintenance
