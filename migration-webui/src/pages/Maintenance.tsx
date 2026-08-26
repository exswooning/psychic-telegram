import React, { useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, TextField, Button, Alert,
} from '@mui/material'
import { Build as MaintenanceIcon } from '@mui/icons-material'
import { fetchActions, fetchConfig, runResetDriveLedger, ActionSpec } from '@/api/client'
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
const MAINTENANCE_KEYS = [
  'resolve_dry', 'resolve', 'repair_modified_times', 'backfill_drive',
  'undo_dry', 'undo',
]

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

      <ResetDriveLedgerCard />
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

export default Maintenance
