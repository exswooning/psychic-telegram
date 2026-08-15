import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Avatar, Box, Button, Checkbox, Chip, CircularProgress, Collapse,
  FormControlLabel, MenuItem, Paper, Stack, Switch, TextField, Typography,
} from '@mui/material'
import {
  RocketLaunch as QuickIcon, Grass as SeedIcon, ContentCopy as CopyIcon,
  CheckCircle as OkIcon, UploadFile as UploadIcon, OpenInNew as OpenIcon,
  PersonAddAlt as AddUsersIcon, ArrowBack as BackIcon,
} from '@mui/icons-material'
import {
  FullSetupStatus, startFullSetup, fetchFullSetupStatus,
  startProvision, fetchProvisionStatus, ProvisionStatus,
  fetchTenantConfigStatus, uploadCredentials, TenantConfigStatus,
} from '@/api/controlPlane'
import { runSeed } from '@/api/client'
import ReasonCodeDialog from './ReasonCodeDialog'
import JobProgress from './JobProgress'

const REPO_CLONE_CMD =
  'git clone https://github.com/exswooning/psychic-telegram -b workspace-migrator && cd psychic-telegram'

/** Google's own four-color "G" -- the standard mark used on any real
 * "Sign in with Google" surface. Reproduced here because this literally
 * *is* the front end for automating a real Google sign-in (dwd_helper.py
 * drives the actual accounts.google.com login with what gets typed into
 * this form) -- not a generic brand reference. */
const GoogleG: React.FC<{ size?: number }> = ({ size = 40 }) => (
  <svg width={size} height={size} viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#4285F4" d="M45.12 24.5c0-1.56-.14-3.06-.4-4.5H24v8.51h11.84c-.51 2.75-2.06 5.08-4.39 6.64v5.52h7.11c4.16-3.83 6.56-9.47 6.56-16.17z" />
    <path fill="#34A853" d="M24 46c5.94 0 10.92-1.97 14.56-5.33l-7.11-5.52c-1.97 1.32-4.49 2.1-7.45 2.1-5.73 0-10.58-3.87-12.31-9.07H4.34v5.7C7.96 41.07 15.4 46 24 46z" />
    <path fill="#FBBC05" d="M11.69 28.18C11.25 26.86 11 25.45 11 24s.25-2.86.69-4.18v-5.7H4.34C2.85 17.09 2 20.45 2 24s.85 6.91 2.34 9.88l7.35-5.7z" />
    <path fill="#EA4335" d="M24 10.75c3.23 0 6.13 1.11 8.41 3.29l6.31-6.31C34.91 4.18 29.93 2 24 2 15.4 2 7.96 6.93 4.34 14.12l7.35 5.7c1.73-5.2 6.58-9.07 12.31-9.07z" />
  </svg>
)

// Google's own real sign-in screens render dark regardless of the calling
// app's own theme (the popup/redirect looks the same whether the site
// embedding it is light or dark) -- these are deliberately hardcoded, not
// pulled from Bitport's own theme tokens, so this card matches the actual
// Google sign-in screenshot it was built from rather than drifting with
// whatever theme Bitport itself is in.
const G_BG = '#131314'
const G_TEXT = '#e3e3e3'
const G_TEXT_DIM = '#9aa0a6'
const G_FIELD_SX = {
  '& .MuiOutlinedInput-root': {
    color: G_TEXT,
    '& fieldset': { borderColor: '#5f6368' },
    '&:hover fieldset': { borderColor: '#8e918f' },
    '&.Mui-focused fieldset': { borderColor: '#a8c7fa' },
  },
  '& .MuiInputLabel-root': { color: G_TEXT_DIM },
  '& .MuiInputLabel-root.Mui-focused': { color: '#a8c7fa' },
}
// MUI's default disabled-contained-button style (a faint rgba(0,0,0,..)
// wash) assumes a light surface behind it -- against G_BG it was nearly
// invisible, with no visible button at all where "Next"/submit should be
// until every field was filled. Real Google sign-in's own disabled "Next"
// stays a legible, if muted, pill -- this reproduces that instead.
const G_PILL_BUTTON_SX = {
  borderRadius: 999, px: 4,
  '&.Mui-disabled': { bgcolor: 'rgba(255,255,255,0.12)', color: 'rgba(255,255,255,0.3)' },
}

/**
 * The automatic route's sign-in step -- styled to match Google's own real
 * sign-in screen (dark card, G mark, one field-group per step, pill
 * button) rather than a generic form, since what happens after submitting
 * genuinely is a real Google sign-in: dwd_helper.py drives an actual
 * accounts.google.com login with this email/password, then grants
 * domain-wide delegation and every service scope in one automated pass --
 * nothing further to configure by hand, which is the whole point of the
 * "automatic" route as opposed to the step-by-step manual one below it.
 *
 * Two steps, matching Google's own "which account, then password" flow:
 * domain+email first, password (plus the org ID/dry-run/seed options that
 * were already part of this form) second. All state is lifted to the
 * parent -- this component only owns which step it's showing.
 */
const GoogleStyleAuth: React.FC<{
  side: 'source' | 'target'
  domain: string; setDomain: (v: string) => void
  email: string; setEmail: (v: string) => void
  password: string; setPassword: (v: string) => void
  orgId: string; setOrgId: (v: string) => void
  dryRun: boolean; setDryRun: (v: boolean) => void
  canSubmit: boolean
  submitLabel: string
  onSubmit: () => void
  extraOptions?: React.ReactNode
}> = ({ side, domain, setDomain, email, setEmail, password, setPassword,
       orgId, setOrgId, dryRun, setDryRun, canSubmit, submitLabel, onSubmit,
       extraOptions }) => {
  const [step, setStep] = useState<0 | 1>(0)
  const [showPassword, setShowPassword] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)

  return (
    <Box sx={{ bgcolor: G_BG, borderRadius: 3, p: 4, maxWidth: 450 }}>
      <GoogleG />
      <Typography variant="h4" sx={{ fontWeight: 500, color: G_TEXT, mt: 3, mb: 3 }}>
        Welcome
      </Typography>

      {step === 0 ? (
        <>
          <Stack spacing={2.5}>
            <TextField
              fullWidth label={`${side} domain`} value={domain}
              onChange={(e) => setDomain(e.target.value)}
              placeholder={side === 'source' ? 'c.example.com' : 'a.example.com'}
              autoFocus sx={G_FIELD_SX}
            />
            <TextField
              fullWidth label="Super admin email" value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={`admin@${domain || 'example.com'}`}
              sx={G_FIELD_SX}
            />
          </Stack>
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 4 }}>
            <Button
              variant="contained" sx={G_PILL_BUTTON_SX}
              disabled={!domain.trim() || !email.trim()}
              onClick={() => setStep(1)}
            >
              Next
            </Button>
          </Box>
        </>
      ) : (
        <>
          <Chip
            avatar={<Avatar sx={{ bgcolor: 'primary.main', color: '#fff' }}>
              {email.charAt(0).toUpperCase()}
            </Avatar>}
            label={email}
            onClick={() => setStep(0)}
            deleteIcon={<BackIcon sx={{ fontSize: 16 }} />}
            onDelete={() => setStep(0)}
            sx={{
              mb: 3, bgcolor: 'rgba(255,255,255,0.08)', color: G_TEXT,
              '& .MuiChip-deleteIcon': { color: G_TEXT_DIM },
            }}
          />
          <TextField
            fullWidth label="Enter your password"
            type={showPassword ? 'text' : 'password'}
            value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="off" autoFocus sx={G_FIELD_SX}
          />
          <FormControlLabel
            sx={{ mt: 1, color: G_TEXT_DIM, '& .MuiTypography-root': { fontSize: 14 } }}
            control={
              <Checkbox
                size="small" checked={showPassword}
                onChange={(e) => setShowPassword(e.target.checked)}
                sx={{ color: G_TEXT_DIM, '&.Mui-checked': { color: '#a8c7fa' } }}
              />
            }
            label="Show password"
          />

          <Button size="small" onClick={() => setShowAdvanced((v) => !v)}
                  sx={{ display: 'block', mt: 1, color: '#a8c7fa' }}>
            {showAdvanced ? 'Hide' : 'Show'} advanced options
          </Button>
          <Collapse in={showAdvanced}>
            <Stack spacing={1.5} sx={{ mt: 2 }}>
              <TextField
                fullWidth size="small" label="Org ID (optional)" value={orgId}
                onChange={(e) => setOrgId(e.target.value)} sx={G_FIELD_SX}
              />
              <FormControlLabel
                sx={{ color: G_TEXT_DIM, '& .MuiTypography-root': { fontSize: 14 } }}
                control={
                  <Switch size="small" checked={dryRun}
                          onChange={(e) => setDryRun(e.target.checked)} />
                }
                label="Dry run first (recommended)"
              />
              {extraOptions}
            </Stack>
          </Collapse>

          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mt: 4 }}>
            <Button onClick={() => setStep(0)} sx={{ color: '#a8c7fa' }}>Back</Button>
            <Button
              variant="contained" sx={G_PILL_BUTTON_SX}
              disabled={!canSubmit} onClick={onSubmit}
            >
              {submitLabel}
            </Button>
          </Box>
        </>
      )}
    </Box>
  )
}

/**
 * Domain, admin email, admin password -- but the Cloud project itself is no
 * longer created from here. provision_gcp.py needs an identity with
 * org-level "create a project" rights, which this control plane deliberately
 * never holds (a shared box is the wrong place for that scope). The admin
 * runs provision_gcp.py themselves, on their own machine, with their own
 * gcloud identity -- it is already a standalone script, nothing new to
 * write -- and drops the resulting key into the dropzone below, uploaded by
 * THIS browser tab's own signed-in session. Domain-wide delegation still
 * happens here: that only ever needed a Workspace admin's password and a
 * real (virtual) display, both of which this control plane does have.
 *
 * The password is never kept. It is sent once in this request, the server
 * schema excludes it from the audit log at the type level (see
 * StartFullSetup.admin_password in api_server.py), and this component
 * clears its own input the moment the request is sent -- there is no
 * "remember password" anywhere in this tool.
 */
const QuickTenantSetup: React.FC<{
  side: 'source' | 'target'
  /** Only meaningful for side="source". */
  showSeedOptions?: boolean
  /** Only meaningful for side="target". */
  showProvisionUsers?: boolean
}> = ({ side, showSeedOptions, showProvisionUsers }) => {
  const [domain, setDomain] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [orgId, setOrgId] = useState('')
  const [dryRun, setDryRun] = useState(true)
  const [seed, setSeed] = useState(false)
  const [seedScale, setSeedScale] = useState('small')
  const [createUsers, setCreateUsers] = useState(false)
  const [provisionUsers, setProvisionUsers] = useState(false)
  const [status, setStatus] = useState<FullSetupStatus | null>(null)
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // -- Cloud project & service account key ----------------------------------
  const [tenantCfg, setTenantCfg] = useState<TenantConfigStatus | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [pendingKey, setPendingKey] = useState<Record<string, unknown> | null>(null)
  const [uploadBusy, setUploadBusy] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const refreshTenantCfg = useCallback(() => {
    fetchTenantConfigStatus(side).then(setTenantCfg).catch(() => {})
  }, [side])

  useEffect(() => { refreshTenantCfg() }, [refreshTenantCfg])

  const copy = (text: string, tag: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(tag)
      setTimeout(() => setCopied(null), 2000)
    })
  }

  const handleFile = (file: File) => {
    setUploadError(null)
    file.text().then((text) => {
      try {
        const parsed = JSON.parse(text)
        if (parsed.type !== 'service_account') {
          throw new Error('that file\'s "type" is not "service_account" -- '
            + 'make sure you picked the key provision_gcp.py produced')
        }
        setPendingKey(parsed)
      } catch (e: any) {
        setUploadError(e.message || 'could not read that file as a service-account key')
      }
    })
  }

  const doUpload = async (reason: string) => {
    if (!pendingKey) return
    setUploadBusy(true); setUploadError(null)
    try {
      await uploadCredentials(reason, side, domain.trim(), pendingKey)
      setPendingKey(null)
      refreshTenantCfg()
    } catch (e: any) {
      setUploadError(e.message)
    } finally {
      setUploadBusy(false)
    }
  }

  const cloneCommand = `${REPO_CLONE_CMD}\npython3 provision_gcp.py \\\n`
    + `  --source-domain ${side === 'source' ? (domain.trim() || '<source-domain>') : '<source-domain>'} \\\n`
    + `  --target-domain ${side === 'target' ? (domain.trim() || '<target-domain>') : '<target-domain>'} \\\n`
    + `  --org-id ${orgId.trim() || '<org-id, optional>'} --json`

  const dwdConsoleUrl = 'https://admin.google.com/ac/owl/domainwidedelegation'

  // Standalone "do it now" actions, separate from the setup dialog above --
  // these run after Quick Setup has already succeeded, and hit the same
  // password-free endpoints the step-by-step panels below already use
  // (webui.py's /api/seed, main.py provision-users), not a re-run of
  // full_setup.py. Re-running full_setup.py to seed would force a second,
  // unnecessary browser-based DWD sign-in for something that needs neither
  // a browser nor a password.
  const [postAction, setPostAction] = useState<'seed' | 'provision' | 'maxUsers' | null>(null)
  const [postBusy, setPostBusy] = useState(false)
  const [postError, setPostError] = useState<string | null>(null)
  const [postDone, setPostDone] = useState<string | null>(null)
  // Drives JobProgress below -- "Seed now" and "Add max users" both start
  // webui.py's Job under the name "seed" (see /api/seed in webui.py), so one
  // live-log panel covers both instead of each button getting its own static
  // "started, check Activity" message and nothing further ever appearing here.
  const [jobActive, setJobActive] = useState(false)
  const [seedJobRunning, setSeedJobRunning] = useState(false)
  const [provisionStatus, setProvisionStatus] = useState<ProvisionStatus | null>(null)

  const poll = useCallback(() => {
    fetchFullSetupStatus(side).then(setStatus).catch(() => {})
  }, [side])

  useEffect(() => {
    poll()
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [poll])

  useEffect(() => {
    if (status?.running && !pollRef.current) {
      pollRef.current = setInterval(poll, 4000)
    } else if (!status?.running && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [status?.running, poll])

  const launch = async (reason: string) => {
    setBusy(true); setError(null)
    try {
      // ok:false on an HTTP 200 (a capacity refusal from job_admission.py,
      // or any other execution-time failure _gated() reports this way) is
      // not thrown by cpFetch -- only a non-2xx status is. Has to be
      // checked explicitly, same reason as JobController.tsx's askStart.
      const r = await startFullSetup(reason, side, domain.trim(), email.trim(), password, {
        orgId: orgId.trim(), dryRun,
        seed: showSeedOptions ? seed : false, seedScale,
        createUsers: showSeedOptions ? createUsers : false,
        provisionUsers: showProvisionUsers ? provisionUsers : false,
      })
      if (!r.ok) throw new Error(r.detail || 'could not start')
      setAsk(false)
      poll()
    } catch (e: any) {
      setError(e.message)
    } finally {
      // Cleared on every path, success or failure -- the field never holds
      // a password that has already been sent.
      setPassword('')
      setBusy(false)
    }
  }

  const canLaunch = domain.trim() && email.trim() && password && !status?.running
  const result = status?.result
  const setUpOk = !!result?.ok && !status?.running

  useEffect(() => {
    if (side !== 'target' || !showProvisionUsers) return
    fetchProvisionStatus('target').then(setProvisionStatus).catch(() => {})
  }, [side, showProvisionUsers, postDone])

  const runPostAction = async (reason: string) => {
    setPostBusy(true); setPostError(null)
    try {
      if (postAction === 'seed') {
        setJobActive(false)
        const r = await runSeed(domain.trim(), seedScale, createUsers, false)
        if (!r.ok) throw new Error(r.error || 'seed failed')
        setJobActive(true)
        setPostDone('seed started — live output below')
      } else if (postAction === 'maxUsers') {
        // createUsers forced true, allUsers left undefined -- seed_sandbox.py
        // requires --create-users alongside --create-until-full and refuses
        // it combined with --all-users/--users/--fit-to-licenses.
        setJobActive(false)
        const r = await runSeed(domain.trim(), seedScale, true, false, undefined, true)
        if (!r.ok) throw new Error(r.error || 'could not add users')
        setJobActive(true)
        setPostDone('adding users until full — live output below')
      } else if (postAction === 'provision') {
        await startProvision(reason, 'target', false)
        setPostDone('provisioning started')
        fetchProvisionStatus('target').then(setProvisionStatus).catch(() => {})
      }
      setPostAction(null)
    } catch (e: any) {
      setPostError(e.message)
    } finally {
      setPostBusy(false)
    }
  }

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1, flexWrap: 'wrap', gap: 1 }}>
        <QuickIcon color="action" />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>
          Quick setup — {side}
        </Typography>
        {status?.running && (
          <Chip size="small" color="primary" icon={<CircularProgress size={10} />}
                label="running…" />
        )}
      </Stack>

      {/* -- Cloud project & service account key -- */}
      <Box sx={{ mb: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
          1. Cloud project & service account key
        </Typography>

        {tenantCfg?.hasKey ? (
          <Box sx={{ p: 1.5, borderRadius: 1, bgcolor: 'action.hover' }}>
            <Stack direction="row" spacing={1} alignItems="center">
              <OkIcon fontSize="small" color="success" />
              <Typography variant="body2">Key on file</Typography>
            </Stack>
            <Typography variant="caption" sx={{ display: 'block', mt: 0.5,
                                                fontFamily: 'ui-monospace, monospace' }}>
              client ID {tenantCfg.clientId}
            </Typography>
            {tenantCfg.scopes.length > 0 && (
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1, flexWrap: 'wrap', gap: 1 }}>
                <Button size="small" startIcon={<CopyIcon />}
                        onClick={() => copy(tenantCfg.scopes.join(','), 'scopes')}>
                  {copied === 'scopes' ? 'Copied' : 'Copy DWD scopes'}
                </Button>
                <Button size="small" endIcon={<OpenIcon />}
                        href={dwdConsoleUrl} target="_blank" rel="noreferrer">
                  Open admin.google.com
                </Button>
              </Stack>
            )}
            <Button size="small" sx={{ mt: 1 }}
                    onClick={() => fileInputRef.current?.click()}>
              Replace key
            </Button>
          </Box>
        ) : (
          <>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
              Run this on your own machine (Mac/PC terminal or Cloud Shell) —
              it uses your own gcloud identity, not this server's. It creates
              projects for <strong>both</strong> tenants in one run and
              produces two key files; drop each one into its matching card.
            </Typography>
            <Box sx={{ position: 'relative' }}>
              <Box component="pre" sx={{
                fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
                overflowX: 'auto', whiteSpace: 'pre-wrap', m: 0,
              }}>
                {cloneCommand}
              </Box>
              <Button size="small" startIcon={<CopyIcon />} sx={{ mt: 0.5 }}
                      onClick={() => copy(cloneCommand.replace(/\\\n\s*/g, ' '), 'cmd')}>
                {copied === 'cmd' ? 'Copied' : 'Copy command'}
              </Button>
            </Box>

            <Box
              onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault(); setDragOver(false)
                const f = e.dataTransfer.files?.[0]
                if (f) handleFile(f)
              }}
              onClick={() => fileInputRef.current?.click()}
              sx={{
                mt: 1.5, p: 2, border: '2px dashed', borderRadius: 1, cursor: 'pointer',
                borderColor: dragOver ? 'primary.main' : 'divider',
                bgcolor: dragOver ? 'action.hover' : 'transparent',
                textAlign: 'center',
              }}
            >
              <UploadIcon color="action" />
              <Typography variant="body2" color="text.secondary">
                Drop the {side}-sa.json key here, or click to browse
              </Typography>
            </Box>
          </>
        )}
        <input
          ref={fileInputRef} type="file" accept="application/json,.json"
          style={{ display: 'none' }}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f) }}
        />
        {uploadError && <Alert severity="error" sx={{ mt: 1 }}>{uploadError}</Alert>}
      </Box>

      <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
        2. Domain-wide delegation
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
        Needs a display for the sign-in step — if it stalls waiting on 2FA
        or a captcha, connect over VNC to watch the browser directly (see
        connect_vps.sh). This drives a real Google sign-in, so the step
        below is styled to match it.
      </Typography>

      <GoogleStyleAuth
        side={side}
        domain={domain} setDomain={setDomain}
        email={email} setEmail={setEmail}
        password={password} setPassword={setPassword}
        orgId={orgId} setOrgId={setOrgId}
        dryRun={dryRun} setDryRun={setDryRun}
        canSubmit={!!canLaunch}
        submitLabel={status?.running ? 'Running…' : dryRun ? 'Preview' : `Set up ${side}`}
        onSubmit={() => setAsk(true)}
      />

      {showSeedOptions && (
        <Stack direction="row" spacing={2} sx={{ mt: 0.5, flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
          <FormControlLabel
            control={<Switch checked={seed} onChange={(e) => setSeed(e.target.checked)} />}
            label={<Typography variant="body2">Also seed this tenant</Typography>}
          />
          <Collapse in={seed} orientation="horizontal">
            <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1 }}>
              <TextField select size="small" label="Scale" value={seedScale}
                         onChange={(e) => setSeedScale(e.target.value)}
                         sx={{ width: 110 }}>
                {['tiny', 'small', 'medium', 'large', 'huge'].map((s) => (
                  <MenuItem key={s} value={s}>{s}</MenuItem>
                ))}
              </TextField>
              <FormControlLabel
                control={<Switch checked={createUsers}
                                 onChange={(e) => setCreateUsers(e.target.checked)} />}
                label={<Typography variant="body2">Create users</Typography>}
              />
            </Stack>
          </Collapse>
        </Stack>
      )}

      {showProvisionUsers && (
        <FormControlLabel
          sx={{ mt: 0.5, display: 'block' }}
          control={<Switch checked={provisionUsers}
                           onChange={(e) => setProvisionUsers(e.target.checked)} />}
          label={
            <Typography variant="body2">
              Also create target accounts from the identity map
            </Typography>
          }
        />
      )}

      {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}

      {result && (
        <Box sx={{ mt: 2 }}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Result</Typography>
            <Chip size="small" label={result.ok ? 'ok' : 'failed'}
                  color={result.ok ? 'success' : 'error'}
                  variant={result.ok ? 'outlined' : 'filled'} />
          </Stack>
          <Box component="pre" sx={{
            fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
            overflowX: 'auto', maxHeight: 260, whiteSpace: 'pre-wrap', m: 0,
          }}>
            {result.phases.map((p) =>
              `${p.status === 'ok' ? 'ok  ' : p.status === 'failed' ? 'FAIL' : p.status === 'skipped' ? '--  ' : '..  '} `
              + `${p.name}${p.detail ? '  ' + p.detail : ''}`
            ).join('\n')}
          </Box>
          {result.missingScopes && result.missingScopes.length > 0 && (
            <Alert severity="warning" sx={{ mt: 1 }}>
              Delegation ran but {result.missingScopes.length} scope(s) still
              are not live: {result.missingScopes.map((s) => s.split('/').pop()).join(', ')}.
              Check the Domain-Wide Delegation panel — propagation can lag a
              minute or two, or this may need a manual re-run.
            </Alert>
          )}
        </Box>
      )}

      {setUpOk && showSeedOptions && side === 'source' && (
        <Box sx={{ mt: 2, pt: 2, borderTop: '1px solid', borderColor: 'divider' }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
            Setup done — seed it
          </Typography>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
            <TextField select size="small" label="Scale" value={seedScale}
                       onChange={(e) => setSeedScale(e.target.value)}
                       sx={{ width: 110 }}>
              {['tiny', 'small', 'medium', 'large', 'huge'].map((s) => (
                <MenuItem key={s} value={s}>{s}</MenuItem>
              ))}
            </TextField>
            <FormControlLabel
              control={<Switch checked={createUsers}
                               onChange={(e) => setCreateUsers(e.target.checked)} />}
              label={<Typography variant="body2">Create users</Typography>}
            />
            <Button variant="contained" startIcon={<SeedIcon />}
                    disabled={postBusy || seedJobRunning || !domain.trim()}
                    onClick={() => setPostAction('seed')}>
              Seed now
            </Button>
            <Button variant="outlined" startIcon={<AddUsersIcon />}
                    disabled={postBusy || seedJobRunning || !domain.trim()}
                    onClick={() => setPostAction('maxUsers')}>
              Add max users
            </Button>
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
            "Add max users" creates generated accounts one at a time until
            Google itself refuses one (out of licences) — the reliable
            alternative to a license-count check, which needs the Reports
            API and can lag days behind on a low-usage tenant.
          </Typography>
          <JobProgress active={jobActive} expectedName="seed" onRunningChange={setSeedJobRunning} />
        </Box>
      )}

      {setUpOk && showProvisionUsers && side === 'target' && (
        <Box sx={{ mt: 2, pt: 2, borderTop: '1px solid', borderColor: 'divider' }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
            Setup done — provision accounts
          </Typography>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
            <Button variant="contained" startIcon={<SeedIcon />}
                    disabled={postBusy || provisionStatus?.running}
                    onClick={() => setPostAction('provision')}>
              {provisionStatus?.running ? 'Running…' : 'Provision users now'}
            </Button>
            {provisionStatus && (provisionStatus.created || provisionStatus.total) && (
              <Typography variant="caption" color="text.secondary">
                {provisionStatus.created}/{provisionStatus.total} created
                {provisionStatus.failed ? `, ${provisionStatus.failed} failed` : ''}
              </Typography>
            )}
          </Stack>
        </Box>
      )}

      {postDone && <Alert severity="success" sx={{ mt: 2 }}>{postDone}</Alert>}
      {postError && <Alert severity="error" sx={{ mt: 2 }}>{postError}</Alert>}

      <ReasonCodeDialog
        open={ask} busy={busy} error={error}
        destructive={!dryRun}
        confirmPhrase={dryRun ? undefined : 'SETUP'}
        title={dryRun ? `Preview ${side} setup` : `Set up ${side} tenant`}
        description={
          dryRun ? (
            <>Lists every step without opening a browser.</>
          ) : tenantCfg?.hasKey ? (
            <>
              Opens a browser and signs in as <strong>{email || 'the admin'}</strong> to
              grant domain-wide delegation, using the service-account key
              already on file — no new Cloud project is created.{' '}
              {showSeedOptions && seed && 'Also seeds this tenant with test data. '}
              {showProvisionUsers && provisionUsers && 'Also creates target accounts. '}
              The password is used once and never stored.
            </>
          ) : (
            <>
              No service-account key is on file yet for this tenant, so this
              will try to create the Cloud project <strong>from this
              server</strong> — which will fail here unless it happens to
              have its own authenticated gcloud. Upload a key in step 1
              above first if that's not the case.{' '}
              The password is used once and never stored.
            </>
          )
        }
        onCancel={() => { setAsk(false); setError(null) }}
        onConfirm={launch}
      />

      <ReasonCodeDialog
        open={!!pendingKey} busy={uploadBusy} error={uploadError}
        title={`Use this key for ${side}`}
        description={
          <>Saves this service-account key for the {side} tenant and reads
          back its client ID and required DWD scopes. The key file itself is
          never shown again after this — re-upload if you need to replace it.</>
        }
        onCancel={() => { setPendingKey(null); setUploadError(null) }}
        onConfirm={doUpload}
      />

      <ReasonCodeDialog
        open={postAction !== null} busy={postBusy} error={postError}
        destructive={postAction === 'seed'}
        confirmPhrase={postAction === 'seed' ? 'SEED' : undefined}
        title={
          postAction === 'seed' ? `Seed ${domain || 'the source tenant'}`
          : postAction === 'maxUsers' ? `Add max users to ${domain || 'the source tenant'}`
          : 'Provision target accounts'
        }
        description={
          postAction === 'seed' ? (
            <>Writes test data into <strong>{domain || 'the source tenant'}</strong>.
            No password needed — uses the service account key from setup.</>
          ) : postAction === 'maxUsers' ? (
            <>Creates generated accounts one at a time in{' '}
            <strong>{domain || 'the source tenant'}</strong> until Google itself
            refuses one (out of licences, typically), then seeds data for
            exactly the ones that succeeded. No pre-flight license count —
            that API can lag days behind on a low-usage tenant, so this asks
            Google directly instead.</>
          ) : (
            <>Creates Workspace accounts on the target from the identity map.
            No password needed — uses the service account key from setup.</>
          )
        }
        onCancel={() => { setPostAction(null); setPostError(null) }}
        onConfirm={runPostAction}
      />
    </Paper>
  )
}

export default QuickTenantSetup
