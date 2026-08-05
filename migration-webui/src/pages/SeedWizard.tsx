import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Chip, Button, TextField, Grid, Alert,
  MenuItem, Checkbox, FormControlLabel, LinearProgress, Stack,
  Dialog, DialogActions, DialogContent, DialogTitle,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import {
  fetchStatus, checkStep, fetchActions, runSeed, runResetTarget,
  ActionSpec, StatusPayload,
} from '@/api/client'
import JobRunner from '@/components/JobRunner'

/**
 * Sandbox rehearsal tools: seed a throwaway source tenant with test data,
 * then reset the target tenant for a clean re-test.
 *
 * Split out from the Setup Wizard on purpose. Both used to live as step 7 of
 * that page's single 9-step stepper, mixed in with the real, one-time setup
 * a production migration needs -- so "Seed the source tenant" showed up
 * right next to "Domain-Wide Delegation authorised" as if they were the same
 * kind of task. wizard.py's own RUN_MODES already models this correctly
 * (seed_only / migrate_only / seed_and_migrate are distinct paths); this
 * page is the same separation applied to the guided UI. Step 7's live
 * state (from /api/status) is still shown here for context -- it is the
 * same backend signal, just presented on its own page instead of sharing a
 * stepper with steps that mean something different.
 */

const SeedWizard: React.FC = () => {
  const [status, setStatus] = useState<StatusPayload | null>(null)
  const [actions, setActions] = useState<Record<string, ActionSpec>>({})
  const [checking, setChecking] = useState(false)
  const [checkResult, setCheckResult] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([fetchStatus(), fetchActions()])
      if (s.error) { setLoadError(s.error) } else { setStatus(s); setLoadError(null) }
      setActions(a)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 8000)
    return () => clearInterval(id)
  }, [refresh])

  const seedStep = status?.steps.find((s) => s.title.toLowerCase().includes('seeded'))

  const handleCheck = async () => {
    if (!seedStep) return
    setChecking(true)
    setCheckResult(null)
    try {
      const r = await checkStep(seedStep.n)
      setCheckResult(r.msg || r.error || 'checked')
      await refresh()
    } finally {
      setChecking(false)
    }
  }

  if (loadError) {
    return (
      <Box>
        <Typography variant="h4" sx={{ fontWeight: 700, mb: 2 }}>Seed Wizard</Typography>
        <Alert severity="warning">
          {loadError}. Nothing here is broken -- this page reads live state from
          the migration engine, and it has nothing to read yet.
        </Alert>
      </Box>
    )
  }

  if (!status) return <LinearProgress sx={{ mt: 4 }} />

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
        <Typography variant="h4" sx={{ fontWeight: 700 }}>Seed Wizard</Typography>
        <Button size="small" startIcon={<RefreshIcon />} onClick={refresh}>Refresh</Button>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Rehearsal tools for test tenants only -- separate from the Setup
        Wizard's real, one-time migration setup. None of this touches a
        production tenant's own data.
      </Typography>

      {seedStep && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 2 }}>
          <CardContent sx={{ p: 2.5 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
              <Typography variant="h6" sx={{ fontWeight: 600 }}>{seedStep.title}</Typography>
              <Chip
                size="small" label={seedStep.state}
                color={seedStep.state === 'done' ? 'success' : seedStep.state === 'manual' ? 'warning' : 'default'}
              />
            </Box>
            {seedStep.note && (
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{seedStep.note}</Typography>
            )}
            {/* check_seed_accounts is what actually lists every account the
               seeder will target -- discover_tenant_entries()'s live
               Directory lookup, printed OK/MISS per address -- not
               something /api/status can show on a poll (see
               webui_spa.py's module docstring on why nothing here makes a
               live Google API call outside an explicit action). */}
            {seedStep.actions.length > 0 && (
              <Stack spacing={2} sx={{ mt: 1.5, mb: 1.5 }}>
                {seedStep.actions.map((key) => actions[key] && (
                  <JobRunner key={key} name={key} spec={actions[key]} onDone={refresh} />
                ))}
              </Stack>
            )}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mt: 1 }}>
              <Button variant="outlined" size="small" onClick={handleCheck} disabled={checking}>
                Check this step
              </Button>
              {checkResult && <Typography variant="caption">{checkResult}</Typography>}
            </Box>
          </CardContent>
        </Card>
      )}

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 2 }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>Seed the source tenant</Typography>
          <SeedStep />
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>Reset the target tenant</Typography>
          <ResetTargetStep />
        </CardContent>
      </Card>
    </Box>
  )
}

const SeedStep: React.FC = () => {
  const [confirmDomain, setConfirmDomain] = useState('')
  const [scale, setScale] = useState('small')
  const [createUsers, setCreateUsers] = useState(false)
  const [allUsers, setAllUsers] = useState(false)
  const [reset, setReset] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const start = async () => {
    setErr(null); setMsg(null)
    const r = await runSeed(confirmDomain, scale, createUsers, reset, allUsers)
    if (r.ok) setMsg('seeding started -- watch the Activity Feed')
    else setErr(r.error || 'could not start')
  }

  return (
    <Box>
      <Alert severity="warning" sx={{ mb: 2 }}>
        Writes fabricated data into the SOURCE tenant. Type the domain back to
        confirm -- this is the only thing that gates it.
      </Alert>
      <Grid container spacing={2} alignItems="center">
        <Grid item xs={12} sm={4}>
          <TextField
            fullWidth size="small" label="Type the source domain to confirm"
            value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
          />
        </Grid>
        <Grid item xs={12} sm={3}>
          <TextField
            fullWidth size="small" select label="Scale" value={scale}
            onChange={(e) => setScale(e.target.value)}
          >
            {['tiny', 'small', 'medium', 'large', 'huge'].map((s) => (
              <MenuItem key={s} value={s}>{s}</MenuItem>
            ))}
          </TextField>
        </Grid>
        <Grid item xs={6} sm={2.5}>
          <FormControlLabel
            control={<Checkbox checked={createUsers}
                              onChange={(e) => setCreateUsers(e.target.checked)} />}
            label={<Typography variant="body2">Create users</Typography>}
          />
        </Grid>
        <Grid item xs={6} sm={2.5}>
          <FormControlLabel
            control={<Checkbox checked={reset}
                              onChange={(e) => setReset(e.target.checked)} />}
            label={<Typography variant="body2">Reset first</Typography>}
          />
        </Grid>
        <Grid item xs={12} sm={4}>
          <FormControlLabel
            control={<Checkbox checked={allUsers}
                              onChange={(e) => setAllUsers(e.target.checked)} />}
            label={
              <Typography variant="body2">
                Seed every user the tenant already has (real headcount, not a fixed 5)
              </Typography>
            }
          />
        </Grid>
      </Grid>
      <Button sx={{ mt: 1 }} size="small" variant="contained" onClick={start}>
        Start seeding
      </Button>
      {msg && <Alert severity="success" sx={{ mt: 1 }}>{msg}</Alert>}
      {err && <Alert severity="error" sx={{ mt: 1 }}>{err}</Alert>}
    </Box>
  )
}

/**
 * Empties the TARGET tenant's seeded data, right alongside "seed the
 * source" -- the natural place to want it, since a re-test against a target
 * that already holds a previous run's data does not verify what it looks
 * like it verifies (phases.py's own reconciliation only means something
 * against a target that started empty).
 *
 * Typed-domain gated the same way SeedStep gates the source: nothing here
 * can run without the operator typing the target domain back, the server
 * re-checks it before building the command, and reset_target.py's own guard
 * checks a third time regardless.
 */
const ResetTargetStep: React.FC = () => {
  const [confirmDomain, setConfirmDomain] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const start = async () => {
    setConfirmOpen(false)
    setErr(null); setMsg(null)
    const r = await runResetTarget(confirmDomain)
    if (r.ok) setMsg('reset started -- watch the Activity Feed')
    else setErr(r.error || 'could not start')
  }

  return (
    <Box>
      <Alert severity="error" sx={{ mb: 2 }}>
        Empties the TARGET tenant's seeded Drive/Gmail/Calendar/Chat data --
        not the ledger, and never the source. Do this before a clean re-test,
        not after a real migration you want to keep.
      </Alert>
      <Grid container spacing={2} alignItems="center">
        <Grid item xs={12} sm={5}>
          <TextField
            fullWidth size="small" label="Type the target domain to confirm"
            value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
          />
        </Grid>
        <Grid item xs={12} sm={4}>
          <Button
            color="error" variant="outlined" size="small"
            disabled={!confirmDomain}
            onClick={() => setConfirmOpen(true)}
          >
            Reset target tenant
          </Button>
        </Grid>
      </Grid>
      {msg && <Alert severity="success" sx={{ mt: 1 }}>{msg}</Alert>}
      {err && <Alert severity="error" sx={{ mt: 1 }}>{err}</Alert>}

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <DialogTitle>Empty {confirmDomain}?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            This deletes the seeded Drive files, mail, calendar events and chat
            spaces reset_target.py can find for every mapped user in this
            tenant. It does not touch the source tenant or the migration
            ledger.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button color="error" variant="contained" onClick={start}>
            Empty {confirmDomain}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

export default SeedWizard
