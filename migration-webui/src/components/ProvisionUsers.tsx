import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Box, Button, Chip, LinearProgress, MenuItem, Paper, Stack,
  TextField, Typography,
} from '@mui/material'
import { PersonAdd as ProvisionIcon } from '@mui/icons-material'
import {
  ProvisionStatus, startProvision, fetchProvisionStatus, getOperator,
} from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * Front end for `provision-users` — the command run by hand to recover
 * from the B5 blocker (10 target accounts deleted, re-created one at a
 * time over SSH). This is that same command with a button and a progress
 * bar instead of a terminal.
 *
 * Progress is **polled**, not pushed over the WebSocket, on purpose:
 * provisioning finishes in seconds to low minutes, so a short-lived 2s
 * poll while a run is in flight is cheaper and simpler than teaching the
 * server's ledger tailer (which watches migration.db) about a completely
 * different subprocess and log file. Polling stops the moment the run
 * isn't active, so there is no ongoing cost once it's done.
 *
 * Create-only, same as the CLI: an address that already exists is left
 * exactly as it is, never updated, never deleted. That's provision.py's
 * own guarantee, not re-implemented here — this component only ever
 * shells out to it.
 */
const ProvisionUsers: React.FC = () => {
  const [tenant, setTenant] = useState<'source' | 'target'>('target')
  const [status, setStatus] = useState<ProvisionStatus | null>(null)
  const [ask, setAsk] = useState(false)
  const [dryRun, setDryRun] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = useCallback((t: 'source' | 'target') => {
    fetchProvisionStatus(t).then(setStatus).catch(() => {})
  }, [])

  useEffect(() => {
    poll(tenant)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [tenant, poll])

  useEffect(() => {
    if (status?.running && !pollRef.current) {
      pollRef.current = setInterval(() => poll(tenant), 2000)
    } else if (!status?.running && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [status?.running, tenant, poll])

  const launch = async (reason: string) => {
    setBusy(true); setError(null)
    try {
      await startProvision(reason, tenant, dryRun)
      setAsk(false)
      poll(tenant)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const isAdmin = !!getOperator()
  const pct = status && status.total > 0
    ? Math.round((status.created / status.total) * 100) : 0
  const missing = status ? Math.max(0, status.total - status.created) : 0

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <ProvisionIcon color="action" />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>Provision users</Typography>
        {status?.running && (
          <Chip size="small" color="primary" label={`running · pid ${status.pid}`} />
        )}
      </Stack>

      <Typography variant="caption" color="text.secondary">
        Creates any account in <code>identity_map</code> that is missing on
        the chosen tenant. Never touches an account that already exists —
        same guarantee as running <code>provision-users</code> by hand.
      </Typography>

      <Stack direction="row" spacing={2} sx={{ mt: 2, alignItems: 'center', flexWrap: 'wrap', gap: 2 }}>
        <TextField
          select size="small" label="Tenant" value={tenant} sx={{ width: 140 }}
          onChange={(e) => setTenant(e.target.value as 'source' | 'target')}
        >
          <MenuItem value="target">target</MenuItem>
          <MenuItem value="source">source</MenuItem>
        </TextField>
        <Button
          variant="contained" disabled={!isAdmin || status?.running}
          onClick={() => setAsk(true)}
        >
          {status?.running ? 'Provisioning…' : 'Provision missing users'}
        </Button>
        {!isAdmin && (
          <Typography variant="caption" color="text.secondary">
            Set an operator name to launch.
          </Typography>
        )}
      </Stack>

      {status && status.total > 0 && (
        <Box sx={{ mt: 2 }}>
          <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.5 }}>
            <Typography variant="caption" color="text.secondary">
              {status.created} / {status.total} account(s) present
              {status.failed > 0 && ` · ${status.failed} failed`}
            </Typography>
            <Typography variant="caption" sx={{ fontVariantNumeric: 'tabular-nums' }}>{pct}%</Typography>
          </Stack>
          <LinearProgress
            variant={status.running ? 'indeterminate' : 'determinate'}
            value={pct} color={status.failed > 0 ? 'warning' : 'primary'}
          />
          {!status.running && missing > 0 && (
            <Alert severity="warning" sx={{ mt: 1 }}>
              {missing} account(s) still missing after this run — check the log below.
            </Alert>
          )}
          {!status.running && missing === 0 && status.created === status.total && (
            <Alert severity="success" sx={{ mt: 1 }}>
              All {status.total} account(s) present on {tenant}.
            </Alert>
          )}
        </Box>
      )}

      {status && status.tail.length > 0 && (
        <Box component="pre" sx={{
          mt: 2, fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
          overflowX: 'auto', maxHeight: 160, m: 0, whiteSpace: 'pre-wrap',
        }}>
          {status.tail.join('\n')}
        </Box>
      )}

      <ReasonCodeDialog
        open={ask} busy={busy} error={error}
        title={`Provision missing users on ${tenant}`}
        destructive={!dryRun}
        description={
          <>
            Creates real, licensed accounts for every address in{' '}
            <code>identity_map</code> not already on <code>{tenant}</code>.
            Passwords are generated, shown once in the log, and never stored —
            the migration itself never needs them, it reaches these accounts
            through domain-wide delegation.
            <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
              <Button
                size="small" variant={dryRun ? 'contained' : 'outlined'}
                onClick={() => setDryRun((v) => !v)}
              >
                {dryRun ? 'Dry run: ON (nothing will be created)' : 'Dry run: OFF'}
              </Button>
            </Stack>
          </>
        }
        onCancel={() => { setAsk(false); setError(null) }}
        onConfirm={launch}
      />
    </Paper>
  )
}

export default ProvisionUsers
