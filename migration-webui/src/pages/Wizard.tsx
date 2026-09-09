import React, { useCallback, useEffect, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import {
  Box, Typography, Card, CardContent, CardActionArea,
  Chip, Button, TextField, Grid, Alert, Divider, RadioGroup, FormControlLabel,
  Radio, LinearProgress, Stack, IconButton, Tooltip, Tabs, Tab,
} from '@mui/material'
import {
  Refresh as RefreshIcon, Grass as SeedIcon, RocketLaunch as MigrateIcon,
  ArrowBack as BackIcon,
  AddCircleOutline as NewMigrationIcon,
} from '@mui/icons-material'
import {
  fetchStatus, checkStep, fetchConfig, saveConfig, setRunMode, fetchActions,
  uploadCredential, fetchDwd, checkDwdNow, diagnoseScopes, ActionSpec,
  StatusPayload, ConfigFields, ConfigPayload, DwdPayload, ScopeDiagnosis, UploadKind,
} from '@/api/client'
import { fetchMe } from '@/api/controlPlane'
import JobRunner from '@/components/JobRunner'
import SeedWizard from '@/pages/SeedWizard'
import QuickTenantSetup from '@/components/QuickTenantSetup'

/**
 * One doorway for both "I need a real migration set up" and "I need a test
 * tenant seeded first" -- these used to be two separate pages/nav entries
 * (Setup Wizard, Seed Wizard). Merged so the choice is the first thing you
 * see here, instead of the two purposes living behind unrelated nav items.
 * wizard.py's build_steps() still reports 9 steps (its step 7, "Source
 * seeded (test tenants only)", is how RUN_MODES tracks seed_only /
 * seed_and_migrate skip logic on the backend); the Migrate path below still
 * filters that one out of its own progress bar, since SeedWizard (rendered
 * for the Seed path) already owns that step's real UI.
 *
 * The Seed choice is gated on account.seed_enabled (opt-in per account, see
 * accounts_auth.set_seed_enabled) -- writing fabricated data into a tenant
 * is a rehearsal tool most real production accounts have no reason to want.
 *
 * Every action button in the Migrate path is the same whitelisted ACTIONS
 * entry the operator dashboard's toolbar fires; this page only arranges
 * them behind a state machine that says which one makes sense next.
 */

// wizard.py's step 7. Filtered out of the Migrate path's own progress bar
// -- SeedWizard.tsx owns that step's real UI.
const SEED_STEP_TITLE_MARKER = 'seeded'

type Purpose = 'seed' | 'migrate'
type Step = 'domain' | 'purpose' | 'counterpart' | 'run'

/** Enough to catch a typo, not enough to argue with a real domain.
 *  Deliberately not a strict RFC pattern: this gates a form, and every
 *  over-tight domain regex eventually rejects somebody's valid TLD. */
export function looksLikeDomain(v: string): boolean {
  const d = v.trim().toLowerCase()
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(d) && !d.startsWith('.') && !d.endsWith('.')
}

/**
 * The shell every setup step sits in.
 *
 * Modelled on Google Workspace's own signup ("Let's get started"), which is
 * the flow this one stands next to in the operator's head: one large light
 * heading, one question, a wide field, a pill button -- and a panel on the
 * right. Google fills that panel with marketing. A decorative illustration
 * here would be exactly the filler the rest of this app avoids, so it
 * carries what the wizard is about to do to the tenant, and changes as the
 * answers come in.
 *
 * Every token is the theme's own: Google Sans Flex, #1a73e8, the 999-radius
 * button, the #dadce0 divider. What changes is scale and rhythm -- the page
 * used an h4 at 1.5rem, which reads as a settings pane rather than the
 * front door of a setup.
 */
const WizardShell: React.FC<{
  heading: string
  sub: string
  onBack?: () => void
  aside: React.ReactNode
  children: React.ReactNode
}> = ({ heading, sub, onBack, aside, children }) => (
  <Box sx={{ maxWidth: 1080, mx: 'auto', pt: { xs: 2, md: 6 }, pb: 6 }}>
    {onBack && (
      <Button size="small" startIcon={<BackIcon />} onClick={onBack}
              data-testid="wizard-back" sx={{ mb: 2, ml: -1 }}>
        Back
      </Button>
    )}
    <Grid container spacing={{ xs: 4, md: 8 }} alignItems="flex-start">
      <Grid item xs={12} md={5}>
        <Typography
          component="h1"
          sx={{
            fontFamily: '"Google Sans Flex", "Roboto", sans-serif',
            fontSize: { xs: '2rem', md: '2.75rem' },
            fontWeight: 400, lineHeight: 1.15, letterSpacing: '-0.5px',
            mb: 1.5, wordBreak: 'break-word',
          }}>
          {heading}
        </Typography>
        <Typography variant="body1" color="text.secondary" sx={{ mb: 4 }}>
          {sub}
        </Typography>
        {children}
      </Grid>
      <Grid item xs={12} md={7}>
        <Box sx={{
          bgcolor: 'background.default',
          border: '1px solid', borderColor: 'divider',
          borderRadius: 4, p: { xs: 3, md: 4 },
        }}>
          {aside}
        </Box>
      </Grid>
    </Grid>
  </Box>
)

/** The right-hand panel's content: a real sequence, numbered because it IS
 *  ordered, not because numbers look tidy. */
const WhatHappens: React.FC<{ title: string; steps: string[]; note?: string }> =
  ({ title, steps, note }) => (
    <>
      <Typography variant="subtitle2" color="text.secondary"
                  sx={{ textTransform: 'uppercase', letterSpacing: '0.8px', mb: 2 }}>
        {title}
      </Typography>
      <Stack spacing={2.5}>
        {steps.map((t, i) => (
          <Stack key={t} direction="row" spacing={2} alignItems="flex-start">
            <Box sx={{
              flexShrink: 0, width: 26, height: 26, borderRadius: 999,
              bgcolor: 'primary.light', color: 'primary.dark',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 13, fontWeight: 500, mt: '1px',
            }}>{i + 1}</Box>
            <Typography variant="body2" sx={{ lineHeight: 1.6 }}>{t}</Typography>
          </Stack>
        ))}
      </Stack>
      {note && (
        <Typography variant="caption" color="text.secondary"
                    sx={{ display: 'block', mt: 3, pt: 2.5,
                          borderTop: '1px solid', borderColor: 'divider' }}>
          {note}
        </Typography>
      )}
    </>
  )

const DomainStep: React.FC<{
  heading: string
  sub: string
  label: string
  initial?: string
  /** Rejected as the answer, because it is the other side of the pair. */
  taken?: string
  aside: React.ReactNode
  onBack?: () => void
  onNext: (domain: string) => void
}> = ({ heading, sub, label, initial = '', taken, aside, onBack, onNext }) => {
  const [value, setValue] = useState(initial)
  const d = value.trim().toLowerCase()
  const same = !!taken && d === taken.trim().toLowerCase()
  const shaped = looksLikeDomain(value)
  // Only complain once there is something to complain about -- an error
  // under an empty field the user has not reached yet is noise.
  const error = value.trim() === '' ? ''
    : same ? 'That is the same tenant. A migration needs two different domains.'
    : !shaped ? 'That does not look like a domain (example: acme.com).'
    : ''

  return (
    <WizardShell heading={heading} sub={sub} onBack={onBack} aside={aside}>
      <TextField
        fullWidth autoFocus label={label} value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && shaped && !same) onNext(d) }}
        error={!!error} helperText={error || ' '}
        inputProps={{ 'data-testid': 'wizard-domain', spellCheck: false,
                      autoCapitalize: 'none', autoCorrect: 'off' }}
        sx={{ '& .MuiOutlinedInput-root': { height: 56 } }}
      />
      <Button variant="contained" size="large" sx={{ mt: 2, px: 4 }}
              data-testid="wizard-domain-next"
              disabled={!shaped || same} onClick={() => onNext(d)}>
        Continue
      </Button>
    </WizardShell>
  )
}

const Wizard: React.FC = () => {
  const [params] = useSearchParams()
  const [step, setStep] = useState<Step>('domain')
  // The tenant this wizard is setting up, and -- for a migration -- the one
  // it is moving into. Held here rather than inside each sub-wizard so the
  // question is asked once, at the front, instead of once per panel.
  const [domain, setDomain] = useState('')
  const [otherDomain, setOtherDomain] = useState('')
  const [purpose, setPurpose] = useState<Purpose | null>(null)
  const [picked, setPicked] = useState<Purpose | ''>('')
  const [seedEnabled, setSeedEnabled] = useState(false)

  useEffect(() => {
    fetchMe().then((a) => setSeedEnabled(a.seed_enabled)).catch(() => {})
  }, [])

  // A ?mode=seed deep link still has to answer "which domain" first, and
  // still only takes effect once seed_enabled is confirmed -- the URL is
  // client-controlled and the entitlement is not known on first render.
  useEffect(() => {
    if (seedEnabled && params.get('mode') === 'seed') setPurpose('seed')
  }, [seedEnabled, params])

  if (step === 'domain') {
    return (
      <DomainStep
        heading="Let's get started"
        sub="Which domain are you setting up? Everything after this is about this tenant."
        label="Domain" initial={domain}
        aside={<WhatHappens
          title="What this sets up"
          steps={[
            'A throwaway Google Cloud project for this tenant, with the APIs it needs enabled.',
            'A service account, and the domain-wide delegation grant that lets it act for your users.',
            'Either a rehearsal corpus of fabricated data, or a real migration into a second tenant.',
          ]}
          note="Nothing is created until you confirm on a later step." />}
        onNext={(d) => {
          setDomain(d)
          // A confirmed deep link skips straight past the question it
          // already answered.
          if (purpose === 'seed' && seedEnabled) { setStep('run'); return }
          setStep('purpose')
        }} />
    )
  }

  if (step === 'purpose') {
    const go = () => {
      if (!picked) return
      setPurpose(picked)
      setStep(picked === 'migrate' ? 'counterpart' : 'run')
    }
    return (
      <WizardShell
        heading={domain}
        sub="What is this domain for?"
        onBack={() => setStep('domain')}
        aside={<WhatHappens
          title={picked === 'migrate' ? 'A real migration'
                 : picked === 'seed' ? 'A rehearsal' : 'The two paths'}
          steps={picked === 'migrate' ? [
            `${domain} is read, never written — the credential for it is read-only.`,
            'You name a destination tenant next, and both get a Cloud project and a delegation grant.',
            'Users are matched, then Drive, Gmail, Calendar, contacts and tasks are copied.',
          ] : picked === 'seed' ? [
            `Fabricated users, files, mail and events are written into ${domain}.`,
            'Nothing real is touched — this is a sandbox corpus for rehearsing a migration.',
            'You can wipe it and reseed as often as you like.',
          ] : [
            'Seed fills a sandbox tenant with fabricated data, so a migration can be rehearsed end to end.',
            'Migrate moves a real tenant into another one, reading the source and writing only the target.',
          ]}
          note={picked === 'seed'
            ? 'Seeding writes data. It is only offered on accounts opted in to it.'
            : undefined} />}>
        <RadioGroup value={picked} onChange={(e) => setPicked(e.target.value as Purpose)}>
          {seedEnabled && (
            <FormControlLabel value="seed" sx={{ mb: 1, alignItems: 'flex-start' }}
              control={<Radio inputProps={{ 'data-testid': 'purpose-seed' } as never}
                              sx={{ pt: 0.5 }} />}
              label={
                <Box sx={{ py: 0.5 }}>
                  <Typography variant="body1" sx={{ fontWeight: 500 }}>
                    Seed it with test data
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    A rehearsal corpus. None of this touches real data.
                  </Typography>
                </Box>
              } />
          )}
          <FormControlLabel value="migrate" sx={{ alignItems: 'flex-start' }}
            control={<Radio inputProps={{ 'data-testid': 'purpose-migrate' } as never}
                            sx={{ pt: 0.5 }} />}
            label={
              <Box sx={{ py: 0.5 }}>
                <Typography variant="body1" sx={{ fontWeight: 500 }}>
                  Migrate it into another tenant
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  gcloud, projects, credentials, delegation, and the real copy.
                </Typography>
              </Box>
            } />
        </RadioGroup>
        <Button variant="contained" size="large" sx={{ mt: 3, px: 4 }}
                data-testid="purpose-next" disabled={!picked} onClick={go}>
          Continue
        </Button>
      </WizardShell>
    )
  }

  if (step === 'counterpart') {
    return (
      <DomainStep
        heading="Where is it going?"
        sub={`${domain} is the source — it is read, never written. Which tenant should its data land in?`}
        label="Destination domain" initial={otherDomain} taken={domain}
        aside={<WhatHappens
          title="After this"
          steps={[
            `Both ${domain} and the destination get a Cloud project, a service account and a delegation grant.`,
            'Users are matched between the two tenants, and you review the mapping before anything copies.',
            'The copy runs per user, per service, and is resumable — it can be stopped and restarted.',
          ]}
          note="The source credential is read-only, so a migration cannot alter the tenant it reads." />}
        onBack={() => setStep('purpose')}
        onNext={(d) => { setOtherDomain(d); setStep('run') }} />
    )
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <Button size="small" startIcon={<BackIcon />}
                data-testid="wizard-change"
                onClick={() => setStep('domain')}>
          Change
        </Button>
        <Chip size="small" variant="outlined" label={domain} />
        {purpose === 'migrate' && otherDomain && (
          <>
            <Typography variant="caption" color="text.secondary">into</Typography>
            <Chip size="small" variant="outlined" label={otherDomain} />
          </>
        )}
      </Stack>
      {purpose === 'seed'
        ? <SeedWizard sourceDomain={domain} />
        : <MigrateWizard sourceDomain={domain} targetDomain={otherDomain} />}
    </Box>
  )
}

/** Automated (Sign in with Google, drives full_setup.py end to end) vs.
 * Manual (the step-by-step flow below) -- same choice SeedWizard.tsx
 * already offers, brought here since a real migration needs the same
 * gcloud + Cloud project + domain-wide delegation work SeedWizard's
 * QuickTenantSetup already automates, just for BOTH tenants instead of
 * only the source. Manual stays the fallback for when the automated
 * sign-in stalls on 2FA/captcha, same reasoning as SeedWizard's own. */
const MigrateWizard: React.FC<{
  sourceDomain?: string
  targetDomain?: string
}> = ({ sourceDomain, targetDomain }) => {
  const [route, setRoute] = useState<'automated' | 'manual'>('automated')
  const navigate = useNavigate()

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={2} sx={{ mb: 0.5 }}>
        <Typography variant="h4" sx={{ fontWeight: 700 }}>
          {sourceDomain && targetDomain
            ? `${sourceDomain} → ${targetDomain}`
            : 'Set up for a real migration'}
        </Typography>
        <Box sx={{ flex: 1 }} />
        {/* Goes to Migrations rather than resetting this form. Wiping the
            fields would look like starting something while actually
            destroying the setup already on screen; the list is where an
            existing pair is picked or a genuinely new one begins. */}
        <Button variant="contained" startIcon={<NewMigrationIcon />}
                data-testid="start-new-migration"
                onClick={() => navigate('/migrations')}>
          Start a new migration
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Automated signs in and handles the Cloud project and delegation for
        both tenants; Manual walks through each part by hand -- use it if
        the automated sign-in stalls on 2FA or a captcha.
      </Typography>

      <Tabs value={route} onChange={(_, v) => setRoute(v)} sx={{ mb: 3, borderBottom: '1px solid', borderColor: 'divider' }}>
        <Tab value="automated" label="Automated" />
        <Tab value="manual" label="Manual" />
      </Tabs>

      {route === 'automated' && (
        <Grid container spacing={2}>
          <Grid item xs={12} md={6}>
            <QuickTenantSetup side="source" view="automated"
                             initialDomain={sourceDomain}
                             onRequestManual={() => setRoute('manual')} />
          </Grid>
          <Grid item xs={12} md={6}>
            <QuickTenantSetup side="target" view="automated" showProvisionUsers
                             initialDomain={targetDomain}
                             onRequestManual={() => setRoute('manual')} />
          </Grid>
        </Grid>
      )}

      {route === 'manual' && <ManualMigrateSteps />}
    </Box>
  )
}

const ManualMigrateSteps: React.FC = () => {
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
      <Alert severity="warning">
        {loadError}. Nothing here is broken -- this page reads live state from
        the migration engine, and it has nothing to read yet.
      </Alert>
    )
  }

  if (!status) return <LinearProgress sx={{ mt: 4 }} />

  // Real migration setup only -- see the module docstring above for why
  // step 7 (sandbox seeding) is excluded here and lives in the Seed path
  // instead.
  const setupSteps = status.steps.filter(
    (s) => !s.title.toLowerCase().includes(SEED_STEP_TITLE_MARKER))
  const stepIdx = Math.max(0, setupSteps.findIndex((s) => s.n === active))
  const step = setupSteps[stepIdx] ?? setupSteps[0]
  const done = setupSteps.filter((s) => s.state === 'done').length
  const total = setupSteps.filter((s) => s.state !== 'skip').length
  const pct = total > 0 ? Math.round((done / total) * 100) : 0

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', mb: 1 }}>
        <Typography variant="body2" color="text.secondary" sx={{ flexGrow: 1 }}>
          {done} of {total} steps satisfied
          {status.users_total > 0 && ` · ${status.users_done} of ${status.users_total} users done`}
        </Typography>
        <Tooltip title="Refresh">
          <IconButton size="small" onClick={refresh}><RefreshIcon fontSize="small" /></IconButton>
        </Tooltip>
      </Box>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 2 }}>
        <CardContent sx={{ p: 2 }}>
          <LinearProgress variant="determinate" value={pct} sx={{ height: 8, borderRadius: 4 }} />
          <Stack direction="row" justifyContent="space-between" sx={{ mt: 0.75 }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              Step {step.n} of {total}: {step.title}
              {step.skipped && (
                <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                  skipped
                </Typography>
              )}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>
              {pct}%
            </Typography>
          </Stack>
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
            <Button
              disabled={stepIdx <= 0}
              onClick={() => setActive(setupSteps[stepIdx - 1].n)}
            >
              &larr; Back
            </Button>
            <Button
              variant="contained"
              disabled={stepIdx >= setupSteps.length - 1}
              onClick={() => setActive(setupSteps[stepIdx + 1].n)}
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
 * reading the help text render nothing extra here. Seeding/reset-target
 * used to live at n===7/8 -- moved to SeedWizard.tsx, see the module
 * docstring above. */
const StepBody: React.FC<{ n: number; onChanged: () => void }> = ({ n, onChanged }) => {
  switch (n) {
    case 2: return <ConfigStep onChanged={onChanged} />
    case 3: return <CredentialsStep onChanged={onChanged} />
    case 5: return <DelegationStep />
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
    // setRunMode() only returns {ok, run_mode, msg} -- it never updates
    // this component's own `cfg` state, so the RadioGroup's `value` kept
    // reading the stale run_mode on every re-render and the click appeared
    // to do nothing (or silently reverted). Refetching is what actually
    // picks up the saved value, the same way `save()` above relies on
    // `onChanged()` to refresh the surrounding wizard's status.
    const r = await setRunMode(mode)
    if (r.ok) {
      const fresh = await fetchConfig()
      setCfg(fresh)
      onChanged()
    }
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
  const [diagnosing, setDiagnosing] = useState<'source' | 'target' | null>(null)
  const [diagnosis, setDiagnosis] = useState<Record<string, ScopeDiagnosis>>({})
  const [showFull, setShowFull] = useState<Record<string, boolean>>({})

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

  const diagnose = async (tenant: 'source' | 'target') => {
    setDiagnosing(tenant)
    try {
      const r = await diagnoseScopes(tenant)
      if (r.ok) setDiagnosis((prev) => ({ ...prev, [tenant]: r.diagnosis }))
    } finally {
      setDiagnosing(null)
    }
  }

  const copy = (text: string) => navigator.clipboard?.writeText(text)

  return (
    <Box sx={{ mb: 2 }}>
      {dwd?.tenants.map((t) => {
        const diag = diagnosis[t.side]
        return (
          <Box sx={{ mb: 2 }} key={t.side}>
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
            <Button
              size="small" sx={{ mt: 0.5 }}
              onClick={() => diagnose(t.side as 'source' | 'target')}
              disabled={diagnosing === t.side}
            >
              {diagnosing === t.side ? 'Checking each scope…' : 'Diagnose scopes'}
            </Button>
            {diag && (
              <Box sx={{ mt: 1 }}>
                {diag.error && !diag.combined_ok && (
                  <Alert severity="warning" sx={{ mb: 1, fontSize: 12 }}>
                    Combined request failed: {diag.error}
                  </Alert>
                )}
                {diag.combined_ok ? (
                  <Alert severity="success" sx={{ fontSize: 12 }}>
                    All {diag.scopes.length} scope(s) authorised.
                  </Alert>
                ) : (
                  <Stack spacing={0.5}>
                    {diag.scopes.map((s) => (
                      <Box key={s.scope} sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Chip
                          size="small" label={s.ok ? 'OK' : 'FAIL'}
                          color={s.ok ? 'success' : 'error'}
                        />
                        <Typography variant="caption" sx={{ fontFamily: 'ui-monospace, monospace' }}>
                          {s.scope}
                        </Typography>
                      </Box>
                    ))}
                  </Stack>
                )}
              </Box>
            )}
            {(() => {
              const full = t.side === 'source' ? dwd.migrate_source_full : dwd.migrate_target_full
              if (!full || !full.length) return null
              const open = !!showFull[t.side]
              const line = full.join(',')
              return (
                <Box sx={{ mt: 1 }}>
                  <Button
                    size="small"
                    onClick={() => setShowFull((prev) => ({ ...prev, [t.side]: !prev[t.side] }))}
                  >
                    {open ? 'Hide' : 'Show'} MIGRATE {t.side.toUpperCase()} full key
                    ({full.length} scopes, every feature toggle, paste once)
                  </Button>
                  {open && (
                    <Box
                      component="pre"
                      sx={{ fontSize: 11, p: 1, mt: 0.5, bgcolor: 'action.hover',
                           borderRadius: 1, overflowX: 'auto', cursor: 'pointer' }}
                      onClick={() => copy(line)}
                      title="Click to copy"
                    >
                      {line}
                    </Box>
                  )}
                </Box>
              )
            })()}
          </Box>
        )
      })}
      <Button size="small" variant="outlined" onClick={check} disabled={checking}>
        Check delegation now
      </Button>
      {result && <Typography variant="caption" sx={{ ml: 1 }}>{result}</Typography>}
    </Box>
  )
}

export default Wizard
