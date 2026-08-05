import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Stepper, Step, StepButton, StepLabel, Card, CardContent,
  Chip, Button, TextField, Grid, Alert, Divider, RadioGroup, FormControlLabel,
  Radio, MenuItem, Checkbox, LinearProgress, Stack,
  Dialog, DialogActions, DialogContent, DialogTitle,
} from '@mui/material'
import {
  CheckCircle as DoneIcon, RadioButtonUnchecked as TodoIcon,
  WarningAmber as ManualIcon, Refresh as RefreshIcon,
} from '@mui/icons-material'
import {
  fetchStatus, checkStep, fetchConfig, saveConfig, setRunMode, fetchActions,
  uploadCredential, fetchDwd, checkDwdNow, runSeed, runResetTarget, ActionSpec,
  StatusPayload, ConfigFields, ConfigPayload, DwdPayload, UploadKind,
} from '@/api/client'
import JobRunner from '@/components/JobRunner'

/**
 * The guided setup path, in the React app.
 *
 * Reuses the exact 9-step model webui.py's inline-JS wizard already drives --
 * wizard.py's build_steps() via /api/status -- rather than a second
 * implementation of what "done" means for each step. Every action button here
 * is the same whitelisted ACTIONS entry the operator dashboard's toolbar
 * fires; this page only arranges them behind a state machine that says which
 * one makes sense next.
 */

const STATE_ICON: Record<string, React.ReactElement> = {
  done: <DoneIcon color="success" fontSize="small" />,
  manual: <ManualIcon color="warning" fontSize="small" />,
  todo: <TodoIcon color="disabled" fontSize="small" />,
  skip: <TodoIcon color="disabled" fontSize="small" />,
}

const Wizard: React.FC = () => {
  const [status, setStatus] = useState<StatusPayload | null>(null)
  const [active, setActive] = useState(1)
  const [actions, setActions] = useState<Record<string, ActionSpec>>({})
  const [checking, setChecking] = useState(false)
  const [checkResult, setCheckResult] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([fetchStatus(), fetchActions()])
      if (s.error) {
        setLoadError(s.error)
      } else {
        setStatus(s)
        setLoadError(null)
      }
      setActions(a)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    // Slower than the dashboard polls: this page is driven by explicit
    // clicks (Check this step / run an action), not by a live migration's
    // second-to-second progress.
    const id = setInterval(refresh, 8000)
    return () => clearInterval(id)
  }, [refresh])

  const handleCheck = async (n: number) => {
    setChecking(true)
    setCheckResult(null)
    try {
      const r = await checkStep(n)
      setCheckResult(r.msg || r.error || 'checked')
      await refresh()
    } finally {
      setChecking(false)
    }
  }

  if (loadError) {
    return (
      <Box>
        <Typography variant="h4" sx={{ fontWeight: 700, mb: 2 }}>Setup Wizard</Typography>
        <Alert severity="warning">
          {loadError}. Nothing here is broken -- this page reads live state from
          the migration engine, and it has nothing to read yet.
        </Alert>
      </Box>
    )
  }

  if (!status) return <LinearProgress sx={{ mt: 4 }} />

  const step = status.steps.find((s) => s.n === active) ?? status.steps[0]

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
        <Typography variant="h4" sx={{ fontWeight: 700 }}>Setup Wizard</Typography>
        <Button size="small" startIcon={<RefreshIcon />} onClick={refresh}>Refresh</Button>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {status.done} of {status.total} steps satisfied
        {status.users_total > 0 && ` · ${status.users_done} of ${status.users_total} users done`}
      </Typography>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 2 }}>
        <CardContent sx={{ p: 2 }}>
          <Stepper nonLinear activeStep={active - 1} alternativeLabel>
            {status.steps.map((s) => (
              <Step key={s.n} completed={s.state === 'done'}>
                <StepButton onClick={() => setActive(s.n)}>
                  <StepLabel
                    icon={STATE_ICON[s.state]}
                    optional={s.skipped ? (
                      <Typography variant="caption" color="text.secondary">skipped</Typography>
                    ) : undefined}
                  >
                    <Typography variant="caption" sx={{ fontWeight: active === s.n ? 700 : 400 }}>
                      {s.title}
                    </Typography>
                  </StepLabel>
                </StepButton>
              </Step>
            ))}
          </Stepper>
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent sx={{ p: 3 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
            <Typography variant="h6" sx={{ fontWeight: 600 }}>
              Step {step.n} · {step.title}
            </Typography>
            <Chip
              size="small"
              label={step.state}
              color={step.state === 'done' ? 'success' : step.state === 'manual' ? 'warning' : 'default'}
            />
          </Box>
          {step.note && (
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
              {step.note}
            </Typography>
          )}
          {step.help.length > 0 && (
            <Box
              component="pre"
              sx={{
                whiteSpace: 'pre-wrap', fontFamily: 'inherit', fontSize: 13,
                color: 'text.secondary', bgcolor: 'background.default',
                border: '1px solid', borderColor: 'divider', borderRadius: 1,
                p: 1.5, mb: 2,
              }}
            >
              {step.help.join('\n')}
            </Box>
          )}
          {step.auto && (
            <Box
              component="pre"
              sx={{
                fontSize: 12, fontFamily: 'ui-monospace, monospace', p: 1, mb: 2,
                bgcolor: 'action.hover', borderRadius: 1, overflowX: 'auto',
              }}
            >
              {step.auto}
            </Box>
          )}

          <StepBody n={step.n} onChanged={refresh} />

          {step.actions.length > 0 && (
            <>
              <Divider sx={{ my: 2 }} />
              <Stack spacing={2}>
                {step.actions.map((key) => actions[key] && (
                  <JobRunner key={key} name={key} spec={actions[key]} onDone={refresh} />
                ))}
              </Stack>
            </>
          )}

          <Divider sx={{ my: 2 }} />
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
            <Button
              variant="outlined" size="small" onClick={() => handleCheck(step.n)}
              disabled={checking}
            >
              Check this step
            </Button>
            {checkResult && <Typography variant="caption">{checkResult}</Typography>}
          </Box>

          <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 3 }}>
            <Button disabled={active <= 1} onClick={() => setActive(active - 1)}>
              &larr; Back
            </Button>
            <Button
              variant="contained"
              disabled={active >= status.steps.length}
              onClick={() => setActive(active + 1)}
            >
              Next &rarr;
            </Button>
          </Box>
        </CardContent>
      </Card>
    </Box>
  )
}

/** Per-step interactive controls. Steps with nothing to fill in beyond
 * reading the help text (1, 9) render nothing extra here. */
const StepBody: React.FC<{ n: number; onChanged: () => void }> = ({ n, onChanged }) => {
  switch (n) {
    case 2: return <ConfigStep onChanged={onChanged} />
    case 3: return <CredentialsStep onChanged={onChanged} />
    case 5: return <DelegationStep />
    case 7: return <SeedStep />
    case 8: return <ResetTargetStep />
    default: return null
  }
}

const ConfigStep: React.FC<{ onChanged: () => void }> = ({ onChanged }) => {
  const [cfg, setCfg] = useState<ConfigPayload | null>(null)
  const [fields, setFields] = useState<ConfigFields>({
    source_domain: '', target_domain: '', source_admin: '', target_admin: '',
  })
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    fetchConfig().then((c) => { setCfg(c); setFields(c.config) })
  }, [])

  const save = async () => {
    setSaving(true); setErr(null); setMsg(null)
    try {
      const r = await saveConfig(fields)
      if (r.ok) { setMsg(r.msg || 'saved'); onChanged() } else { setErr(r.error || 'save failed') }
    } finally {
      setSaving(false)
    }
  }

  const pickMode = async (mode: string) => {
    const r = await setRunMode(mode)
    if (r.ok) onChanged()
  }

  return (
    <Box sx={{ mb: 2 }}>
      <Grid container spacing={2} sx={{ mb: 2 }}>
        {(['source_domain', 'target_domain', 'source_admin', 'target_admin'] as const).map((f) => (
          <Grid item xs={12} sm={6} key={f}>
            <TextField
              fullWidth size="small" label={f.replace('_', ' ')}
              value={fields[f]}
              onChange={(e) => setFields({ ...fields, [f]: e.target.value })}
            />
          </Grid>
        ))}
      </Grid>
      <Button variant="contained" size="small" onClick={save} disabled={saving}>
        Save configuration
      </Button>
      {msg && <Alert severity="success" sx={{ mt: 1 }}>{msg}</Alert>}
      {err && <Alert severity="error" sx={{ mt: 1 }}>{err}</Alert>}

      {cfg && (
        <>
          <Divider sx={{ my: 2 }} />
          <Typography variant="subtitle2" sx={{ mb: 1 }}>Run mode</Typography>
          <RadioGroup value={cfg.run_mode} onChange={(e) => pickMode(e.target.value)}>
            {Object.entries(cfg.run_modes).map(([key, spec]) => (
              <FormControlLabel
                key={key} value={key} control={<Radio size="small" />}
                label={
                  <Box>
                    <Typography variant="body2">{spec.label}</Typography>
                    <Typography variant="caption" color="text.secondary">{spec.blurb}</Typography>
                  </Box>
                }
              />
            ))}
          </RadioGroup>
        </>
      )}
    </Box>
  )
}

const UPLOAD_KINDS: { kind: UploadKind; label: string }[] = [
  { kind: 'source_key', label: 'Source service-account key' },
  { kind: 'target_key', label: 'Target service-account key' },
  { kind: 'oauth_client', label: 'OAuth client (if using OAuth instead)' },
]

const CredentialsStep: React.FC<{ onChanged: () => void }> = ({ onChanged }) => {
  const [uploads, setUploads] = useState<ConfigPayload['uploads']>({})
  const [busy, setBusy] = useState<UploadKind | null>(null)
  const [msg, setMsg] = useState<Record<string, string>>({})

  const load = useCallback(() => {
    fetchConfig().then((c) => setUploads(c.uploads))
  }, [])
  useEffect(load, [load])

  const onFile = async (kind: UploadKind, file: File | undefined) => {
    if (!file) return
    setBusy(kind)
    try {
      const r = await uploadCredential(kind, file)
      setMsg({ ...msg, [kind]: r.ok ? (r.msg || 'saved') : (r.error || 'upload failed') })
      if (r.ok) { load(); onChanged() }
    } finally {
      setBusy(null)
    }
  }

  return (
    <Stack spacing={2} sx={{ mb: 2 }}>
      {UPLOAD_KINDS.map(({ kind, label }) => {
        const status = uploads[kind]
        return (
          <Box key={kind}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <Typography variant="body2" sx={{ flexGrow: 1 }}>{label}</Typography>
              {status?.valid && <Chip size="small" color="success" label="valid" />}
              {status?.present && !status.valid && <Chip size="small" color="error" label="invalid" />}
              <Button component="label" size="small" variant="outlined" disabled={busy === kind}>
                Upload JSON
                <input
                  type="file" accept="application/json" hidden
                  onChange={(e) => onFile(kind, e.target.files?.[0])}
                />
              </Button>
            </Box>
            {status?.warning && <Alert severity="warning" sx={{ mt: 0.5 }}>{status.warning}</Alert>}
            {msg[kind] && (
              <Typography variant="caption" color={status?.valid ? 'success.main' : 'error'}>
                {msg[kind]}
              </Typography>
            )}
          </Box>
        )
      })}
    </Stack>
  )
}

const DelegationStep: React.FC = () => {
  const [dwd, setDwd] = useState<DwdPayload | null>(null)
  const [checking, setChecking] = useState(false)
  const [result, setResult] = useState<string | null>(null)

  useEffect(() => { fetchDwd().then(setDwd) }, [])

  const check = async () => {
    setChecking(true)
    try {
      const r = await checkDwdNow()
      const failed = r.status.steps.filter((s) => s.n === 5)[0]
      setResult(failed?.note || (r.ok ? 'checked' : 'not yet authorised'))
    } finally {
      setChecking(false)
    }
  }

  const copy = (text: string) => navigator.clipboard?.writeText(text)

  return (
    <Box sx={{ mb: 2 }}>
      {dwd?.tenants.map((t) => (
        <Box sx={{ mb: 1.5 }} key={t.side}>
          <Typography variant="caption" color="text.secondary">
            {t.side.toUpperCase()} client ID ({t.client_id}) -- paste the whole
            line into {t.domain}'s Admin Console; it replaces what is there:
          </Typography>
          <Box
            component="pre"
            sx={{ fontSize: 11, p: 1, bgcolor: 'action.hover', borderRadius: 1,
                 overflowX: 'auto', cursor: 'pointer' }}
            onClick={() => copy(t.scopes)}
            title="Click to copy"
          >
            {t.scopes}
          </Box>
        </Box>
      ))}
      <Button size="small" variant="outlined" onClick={check} disabled={checking}>
        Check delegation now
      </Button>
      {result && <Typography variant="caption" sx={{ ml: 1 }}>{result}</Typography>}
    </Box>
  )
}

const SeedStep: React.FC = () => {
  const [confirmDomain, setConfirmDomain] = useState('')
  const [scale, setScale] = useState('small')
  const [createUsers, setCreateUsers] = useState(false)
  const [reset, setReset] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const start = async () => {
    setErr(null); setMsg(null)
    const r = await runSeed(confirmDomain, scale, createUsers, reset)
    if (r.ok) setMsg('seeding started -- watch the Activity Feed')
    else setErr(r.error || 'could not start')
  }

  return (
    <Box sx={{ mb: 2 }}>
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
 * Empties the TARGET tenant's seeded data, right where "run a clean migrate"
 * lives -- the natural place to want it, since a re-test against a target
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
    <Box sx={{ mb: 2 }}>
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

export default Wizard
