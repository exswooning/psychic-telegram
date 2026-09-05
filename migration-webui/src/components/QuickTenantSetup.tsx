import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Avatar, Box, Button, Checkbox, Chip, CircularProgress, Collapse,
  Dialog, FormControlLabel, IconButton, LinearProgress, MenuItem, Paper,
  Stack, Switch, TextField, Typography,
} from '@mui/material'
import {
  RocketLaunch as QuickIcon, Grass as SeedIcon, ContentCopy as CopyIcon,
  CheckCircle as OkIcon, UploadFile as UploadIcon, OpenInNew as OpenIcon,
  PersonAddAlt as AddUsersIcon, ArrowBack as BackIcon, Close as CloseIcon,
} from '@mui/icons-material'
import {
  FullSetupStatus, startFullSetup, fetchFullSetupStatus,
  startProvision, fetchProvisionStatus, ProvisionStatus,
  fetchTenantConfigStatus, uploadCredentials, TenantConfigStatus,
  fetchTenantInventory, TenantInventory,
  startTenantScan, fetchTenantScan,
  buildIdentityMap, fetchIdentityMapStatus, IdentityMapStatus,
} from '@/api/controlPlane'
import { runSeed } from '@/api/client'
import ReasonCodeDialog from './ReasonCodeDialog'
import JobProgress from './JobProgress'
import TenantInventoryPanel from './TenantInventoryPanel'
import ReprovisionPanel from './ReprovisionPanel'

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
  '& .MuiFormHelperText-root': { color: G_TEXT_DIM },
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
 * A modal, not an inline card: the real thing is never embedded mid-page
 * next to unrelated content -- it's a focused overlay. An earlier version
 * inlined this dark card directly into the light "2. Domain-wide
 * delegation" section and it read as a broken/foreign element sitting in
 * the page rather than an intentional sign-in moment. A centered dialog
 * with real elevation is both more authentic to what it's imitating and
 * looks like a deliberate surface instead of a stray black rectangle.
 *
 * Two steps, matching Google's own "which account, then password" flow:
 * domain+email first, password (plus the org ID/dry-run/seed options that
 * were already part of this form) second. All state is lifted to the
 * parent -- this component only owns which step it's showing.
 */
const GoogleStyleAuth: React.FC<{
  open: boolean
  onClose: () => void
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
}> = ({ open, onClose, side, domain, setDomain, email, setEmail, password, setPassword,
       orgId, setOrgId, dryRun, setDryRun, canSubmit, submitLabel, onSubmit,
       extraOptions }) => {
  const [step, setStep] = useState<0 | 1>(0)
  const [showPassword, setShowPassword] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  // A Workspace super admin's email is, in the overwhelming majority of
  // cases, on the domain being administered -- so ask for just the email,
  // like Google's own sign-in does, and derive the domain from it instead
  // of making the user type the same thing twice. Stops auto-deriving the
  // moment the domain field itself gets touched (in advanced options
  // below), so a manual correction for the rare domain-alias case sticks
  // instead of being silently overwritten on the next keystroke in email.
  const domainEditedRef = useRef(false)
  const handleEmailChange = (v: string) => {
    setEmail(v)
    if (!domainEditedRef.current) {
      const at = v.lastIndexOf('@')
      setDomain(at >= 0 ? v.slice(at + 1).trim() : '')
    }
  }

  return (
    <Dialog
      open={open} onClose={onClose} maxWidth="xs" fullWidth
      PaperProps={{ sx: {
        bgcolor: G_BG, borderRadius: 4, p: 4,
        boxShadow: '0 24px 60px rgba(0,0,0,0.45)',
      } }}
    >
      <IconButton
        onClick={onClose} size="small"
        sx={{ position: 'absolute', top: 12, right: 12, color: G_TEXT_DIM }}
      >
        <CloseIcon fontSize="small" />
      </IconButton>
      <GoogleG />
      <Typography variant="h4" sx={{ fontWeight: 500, color: G_TEXT, mt: 3, mb: 3 }}>
        Welcome
      </Typography>

      {step === 0 ? (
        <>
          <Stack spacing={2.5}>
            <TextField
              fullWidth label="Super admin email" value={email}
              onChange={(e) => handleEmailChange(e.target.value)}
              placeholder={side === 'source' ? 'admin@c.example.com' : 'admin@a.example.com'}
              autoFocus sx={G_FIELD_SX}
            />
            {domain.trim() && (
              <Typography variant="caption" sx={{ color: G_TEXT_DIM, mt: -1.5 }}>
                {side} domain: <Box component="span" sx={{ color: G_TEXT, fontWeight: 500 }}>
                  {domain.trim()}
                </Box>
              </Typography>
            )}
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
                fullWidth size="small" label={`${side} domain`} value={domain}
                onChange={(e) => { domainEditedRef.current = true; setDomain(e.target.value) }}
                helperText="auto-filled from your email -- edit if the Workspace domain differs"
                sx={G_FIELD_SX}
              />
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
    </Dialog>
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
  /** 'automated' shows only the Sign in with Google trigger, its dialog,
   * and the direct feedback (running/result/error) from that one action.
   * Everything else -- the Cloud project & key section, the seed/provision
   * toggles, and the post-setup quick actions -- only renders in 'manual',
   * so the automated route stays a single, uncluttered button and every
   * other control lives in one predictable place instead of both. */
  view: 'automated' | 'manual'
  /** Only meaningful for side="source". */
  showSeedOptions?: boolean
  /** Only meaningful for side="target". */
  showProvisionUsers?: boolean
  /** Lets the automated view's "no key yet" notice jump straight to the
   * Manual tab instead of just telling the user where to look. */
  onRequestManual?: () => void
}> = ({ side, view, showSeedOptions, showProvisionUsers, onRequestManual }) => {
  const [domain, setDomain] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [orgId, setOrgId] = useState('')
  const [dryRun, setDryRun] = useState(true)
  const [authDialogOpen, setAuthDialogOpen] = useState(false)
  const [seed, setSeed] = useState(false)
  const [seedScale, setSeedScale] = useState('small')
  const [createUsers, setCreateUsers] = useState(false)
  // Comma-separated localparts. The Seed Wizard's Manual tab has had this
  // since the field was added; the Automated tab -- the default, and the one
  // most people use -- could only ever seed the whole tenant. On this tenant
  // that is 200 users and days, which makes "seed, then check the change I
  // just made" impossible from the path people actually take.
  const [seedUsers, setSeedUsers] = useState('')
  const [provisionUsers, setProvisionUsers] = useState(false)
  const [status, setStatus] = useState<FullSetupStatus | null>(null)
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  // Covers the click-to-first-poll gap, which status?.running cannot: a
  // dry run does no real work at all, so the backend subprocess routinely
  // exits before the frontend's own first status fetch even lands -- ps
  // never catches it "running", and the page jumped straight from the
  // button to a populated Result with nothing in between. Cleared once
  // that first poll actually resolves, by which point either status
  // reflects a real run in flight (and the block below takes over) or
  // the dry-run result is already in and there is nothing left to cover.
  const [justLaunched, setJustLaunched] = useState(false)
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
  const [postAction, setPostAction] = useState<
    'seed' | 'provision' | 'maxUsers' | 'mapUsers' | null>(null)
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
    return fetchFullSetupStatus(side).then(setStatus).catch(() => {})
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

  // Set by the re-provision panel just before it asks for a Reason Code, so
  // the launch below carries the destructive flags. Cleared after every
  // launch so an ordinary setup can never inherit them.
  const reprovisionRef = useRef<{ scopes: string[] } | null>(null)

  const launch = async (reason: string) => {
    setBusy(true); setError(null); setJustLaunched(true)
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
        ...(reprovisionRef.current
          ? {
              reprovision: true,
              // The server re-checks this; sending it is not the gate, the
              // panel already made the operator type it.
              confirmDomain: domain.trim(),
              scopes: reprovisionRef.current.scopes,
            }
          : {}),
      })
      if (!r.ok) throw new Error(r.detail || 'could not start')
      setAsk(false)
      // Awaited, not fire-and-forget: justLaunched must stay true (so the
      // fallback indicator below stays up) until status genuinely reflects
      // this run one way or the other.
      await poll()
    } catch (e: any) {
      setError(e.message)
    } finally {
      // Cleared on every path, success or failure -- the field never holds
      // a password that has already been sent.
      setPassword('')
      setBusy(false)
      setJustLaunched(false)
      // Never let an ordinary setup inherit the destructive flags.
      reprovisionRef.current = null
    }
  }

  const canLaunch = domain.trim() && email.trim() && password && !status?.running
  const result = status?.result
  const setUpOk = !!result?.ok && !status?.running

  // -- Tenant inventory ------------------------------------------------------
  // What is actually in the tenant this setup just pointed at: how many
  // accounts, and how much data each holds. Fetched once when setup
  // succeeds, never on a poll -- it costs two live Google calls per account
  // (~34s on a real 201-account tenant), so it is explicitly triggered and
  // refreshable rather than continuously refreshed.
  const [inv, setInv] = useState<TenantInventory | null>(null)
  const [invBusy, setInvBusy] = useState(false)
  const [invError, setInvError] = useState('')

  // Never replace deep data with shallower data.
  //
  // The quick read and the stored deep scan are fetched together, and they
  // race: the stored scan answers in milliseconds, the quick read walks
  // every account and takes ~30 seconds on a 201-account tenant. So the
  // quick result landed LAST and overwrote a complete scan -- sharing,
  // Chat and calendar columns present one moment and gone the next, which
  // is exactly what it looked like.
  //
  // Guarding the setter rather than ordering the calls, because ordering
  // only fixes the orderings you thought of; a slow network makes a new one.
  const applyInv = useCallback((next: TenantInventory, force = false) => {
    setInv((prev) => (!force && prev?.deep && !next.deep ? prev : next))
  }, [])

  // How old the inventory on screen is. Declared above loadInventory
  // because that is where it is first set.
  const [scanAge, setScanAge] = useState<number | null>(null)

  const loadInventory = useCallback((deep = false, force = false) => {
    setInvBusy(true)
    setInvError('')
    fetchTenantInventory(side, 250, deep)
      .then((r) => applyInv(r, force))
      .catch((e) => setInvError(e instanceof Error ? e.message : String(e)))
      .finally(() => setInvBusy(false))
    // The stored deep scan is what those numbers actually come from, and
    // the only thing that knows when they were true. Asked on every load,
    // not just while a scan is being watched -- a page opened hours later
    // is exactly the case where the age matters.
    fetchTenantScan(side)
      .then((st) => setScanAge(st.ageSeconds ?? null))
      .catch(() => { /* the panel still works without an age */ })
  }, [side, applyInv])

  // Deep scan: start it, then poll. It walks every file every account owns,
  // so it cannot live inside the request that asks for it -- that 502'd.
  const scanPoll = useRef<number | null>(null)

  const [scanProgress, setScanProgress] = useState<{ done: number; total: number } | null>(null)

  const pollScan = useCallback(() => {
    fetchTenantScan(side)
      .then((st) => {
        if (st.running) {
          // Show movement. A scan whose only signal is a spinner is
          // indistinguishable from one that has died -- and they have died.
          setScanProgress({ done: st.done ?? 0, total: st.scanTotal ?? 0 })
          return
        }
        if (!st.present) return
        if (scanPoll.current) { window.clearInterval(scanPoll.current); scanPoll.current = null }
        setInvBusy(false)
        setScanProgress(null)
        setScanAge(st.ageSeconds ?? null)
        if (st.error) setInvError(st.error)
        else applyInv(st as TenantInventory)
      })
      .catch(() => { /* a poll blip must not end the watch */ })
  }, [side, applyInv])

  const runDeepScan = useCallback(() => {
    setInvBusy(true)
    setInvError('')
    startTenantScan(side, 0)
      .then(() => {
        if (scanPoll.current) window.clearInterval(scanPoll.current)
        scanPoll.current = window.setInterval(pollScan, 5000)
      })
      .catch((e) => {
        setInvBusy(false)
        setInvError(e instanceof Error ? e.message : String(e))
      })
  }, [side, pollScan])

  useEffect(() => () => {
    if (scanPoll.current) window.clearInterval(scanPoll.current)
  }, [])

  const invLoadedFor = useRef('')
  useEffect(() => {
    if (!setUpOk) return
    // Once per successful setup, not once per render.
    const key = `${side}:${result?.clientId || ''}`
    if (invLoadedFor.current === key) return
    invLoadedFor.current = key

    // Quick figures first -- they land in seconds and are what the panel is
    // mostly for. Then adopt a completed deep scan if one already exists,
    // and only start a new one when there is nothing stored.
    //
    // Auto-starting is what makes the sharing numbers appear without anyone
    // asking, which is the point; not re-starting when a result is already
    // on disk is what keeps that from re-walking every file in the tenant
    // each time someone opens the wizard.
    loadInventory(false)
    fetchTenantScan(side)
      .then((st) => {
        if (st.running) {
          setInvBusy(true)
          if (scanPoll.current) window.clearInterval(scanPoll.current)
          scanPoll.current = window.setInterval(pollScan, 5000)
        } else if (st.present && !st.error) {
          applyInv(st as TenantInventory)
        } else {
          runDeepScan()
        }
      })
      .catch(() => { /* the quick figures already loaded; leave it at that */ })
  }, [setUpOk, side, result?.clientId, loadInventory, pollScan, runDeepScan])

  const [identityStatus, setIdentityStatus] = useState<IdentityMapStatus | null>(null)
  // Transitions, not levels: a flag alone re-fires on every remount.
  const identityRunningRef = useRef(false)
  const provisionRunningRef = useRef(false)
  const chainProvisionRef = useRef(false)
  const chainReasonRef = useRef('')

  // Same polling shape as provisioning below, for the same reason: listing
  // both directories on a 200-account tenant takes long enough that a
  // one-shot read reports "running" and then never corrects itself.
  useEffect(() => {
    if (side !== 'target' || !showProvisionUsers) return
    let timer: number | null = null
    let stopped = false
    const tick = () => {
      fetchIdentityMapStatus()
        .then((st) => {
          if (stopped) return
          const wasRunning = identityRunningRef.current
          identityRunningRef.current = st.running
          setIdentityStatus(st)
          if (!st.running && timer) { window.clearInterval(timer); timer = null }
          // Mapping only writes identity_map; the accounts still have to be
          // created, and doing that as a separate click meant a target that
          // said "201 mapped" next to "0 created" and looked stuck. Chained
          // on the transition, not on the flag, so re-mounting the panel
          // after a finished map cannot re-trigger it.
          if (wasRunning && !st.running && chainProvisionRef.current) {
            chainProvisionRef.current = false
            startProvision(chainReasonRef.current, 'target', false)
              .then(() => setPostDone('creating the accounts that do not exist yet'))
              .catch((e) => setPostError(e instanceof Error ? e.message : String(e)))
          }
        })
        .catch(() => { /* a blip must not end the watch */ })
    }
    tick()
    timer = window.setInterval(tick, 3000)
    return () => { stopped = true; if (timer) window.clearInterval(timer) }
  }, [side, showProvisionUsers, postDone])

  // Poll while it runs, not once when it starts.
  //
  // This fetched exactly once immediately after launch -- when `ps` still
  // saw the process and the log was empty -- and never again. Provisioning
  // one account takes about two seconds, so the button sat on "Running..."
  // with no rows under it indefinitely, for a job that had already
  // finished and written its result. "provisioning started" was the last
  // true thing the panel ever said.
  useEffect(() => {
    if (side !== 'target' || !showProvisionUsers) return
    let timer: number | null = null
    let stopped = false

    const tick = () => {
      fetchProvisionStatus('target')
        .then((st) => {
          if (stopped) return
          setProvisionStatus(st)
          // One more read after it stops: the process disappears from `ps`
          // slightly before the last lines are flushed to the log, so the
          // poll that first sees running=false can still be a line short.
          if (!st.running && timer) {
            window.clearInterval(timer)
            timer = null
            window.setTimeout(() => {
              if (!stopped) {
                fetchProvisionStatus('target').then(setProvisionStatus).catch(() => {})
              }
            }, 1500)
            // Provisioning is the one action here that CHANGES the tenant,
            // so the panel above it is stale the moment it finishes. It read
            // "1 account" beside "200 created", which is the panel
            // contradicting itself on the same screen.
            if (provisionRunningRef.current) {
              loadInventory(false, true)
            }
          }
          provisionRunningRef.current = st.running
        })
        .catch(() => { /* a blip must not end the watch */ })
    }

    tick()
    timer = window.setInterval(tick, 2000)
    return () => {
      stopped = true
      if (timer) window.clearInterval(timer)
    }
  }, [side, showProvisionUsers, postDone, loadInventory])

  const runPostAction = async (reason: string) => {
    setPostBusy(true); setPostError(null)
    try {
      if (postAction === 'seed') {
        setJobActive(false)
        const r = await runSeed(domain.trim(), seedScale, createUsers, false,
                                undefined, undefined, undefined, undefined,
                                undefined, seedUsers.trim() || undefined)
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
      } else if (postAction === 'mapUsers') {
        // The Reason Code is carried into the provisioning call the chain
        // fires next, so the account creation is audited under the same
        // reason the operator actually gave -- not a synthesised one.
        chainReasonRef.current = reason
        chainProvisionRef.current = true
        identityRunningRef.current = true
        await buildIdentityMap(reason, true)
        setPostDone('mapping users from the source directory, then creating '
                    + 'the ones that do not exist yet')
        fetchIdentityMapStatus().then(setIdentityStatus).catch(() => {})
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

      {/* Real, not simulated: full_setup.py writes {pct, label} to a
       * progress file after every meaningful step (see run_full_setup's
       * _progress() and provision_gcp.provision_side()'s on_step
       * callback), so this tracks the actual gcloud/DWD automation, not a
       * fake animated bar. Scoped to the automated view -- that's the
       * only view that ever starts this job. */}
      {view === 'automated' && status?.running && (
        <Box sx={{ mb: 2 }}>
          {typeof status.progressPct === 'number' ? (
            <LinearProgress variant="determinate" value={status.progressPct} />
          ) : (
            <LinearProgress />
          )}
          <Stack direction="row" justifyContent="space-between" sx={{ mt: 0.5 }}>
            <Typography variant="caption" color="text.secondary">
              {status.progressLabel || 'working…'}
            </Typography>
            {typeof status.progressPct === 'number' && (
              <Typography variant="caption" color="text.secondary"
                          sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {status.progressPct}%
              </Typography>
            )}
          </Stack>
        </Box>
      )}

      {/* Covers a dry run, which the block above never catches: it does no
       * real work, so the backend is routinely already done by the time
       * the first poll lands, and status?.running never once observes
       * true. justLaunched is purely local -- true from the moment the
       * button is clicked until that first poll resolves either way -- so
       * this always renders for at least one round trip, giving a preview
       * (or a real run's own slower start) a visible "something happened"
       * beat instead of jumping straight from button to Result. */}
      {view === 'automated' && justLaunched && !status?.running && (
        <Box sx={{ mb: 2 }}>
          <LinearProgress />
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
            {dryRun ? 'previewing…' : 'starting…'}
          </Typography>
        </Box>
      )}

      {/* -- Cloud project & service account key -- */}
      {view === 'manual' && (
      <Box sx={{ mb: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
          Cloud project & service account key
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
      )}

      {view === 'automated' ? (
        <>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Signs in as the Workspace super admin and grants domain-wide
            delegation automatically. {tenantCfg && !tenantCfg.hasKey && (
              <>No Cloud project is on file for {side} yet, so this same
              sign-in also creates it under the admin's own Google account —
              nothing extra to run or upload. </>
            )}Needs a real browser; if it stalls on 2FA or a captcha, use
            the Manual tab instead.
          </Typography>

          <Button
            variant="outlined"
            onClick={() => setAuthDialogOpen(true)}
            startIcon={<GoogleG size={18} />}
            sx={{
              textTransform: 'none', fontWeight: 500, borderRadius: '4px',
              borderColor: 'divider', color: 'text.primary', px: 2.5, py: 1,
              bgcolor: 'background.paper',
              '&:hover': { borderColor: 'text.secondary', bgcolor: 'action.hover' },
            }}
          >
            {domain.trim() && email.trim() ? `Continue as ${email.trim()}` : 'Sign in with Google'}
          </Button>
          {domain.trim() && email.trim() && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
              {domain.trim()} — click to change or enter the password
            </Typography>
          )}

          <GoogleStyleAuth
            open={authDialogOpen}
            onClose={() => setAuthDialogOpen(false)}
            side={side}
            domain={domain} setDomain={setDomain}
            email={email} setEmail={setEmail}
            password={password} setPassword={setPassword}
            orgId={orgId} setOrgId={setOrgId}
            dryRun={dryRun} setDryRun={setDryRun}
            canSubmit={!!canLaunch}
            submitLabel={status?.running ? 'Running…' : dryRun ? 'Preview' : `Set up ${side}`}
            onSubmit={() => { setAuthDialogOpen(false); setAsk(true) }}
          />
        </>
      ) : (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
          Prefer the automated Google sign-in? Switch to the Automated tab —
          it handles delegation (and the Cloud project, if needed) in one
          step. Use the panels below if that stalls on 2FA/a captcha, or
          you'd rather drive Admin Console yourself.
        </Typography>
      )}

      {/* Not view-gated: the Automated launch() call already threads
       * seed/seedScale/createUsers through to startFullSetup() regardless
       * of view (see below), so Automated needs the same way to set them
       * before a single-click run -- gating this to Manual only left the
       * automated path with no way to opt into seeding in the same run,
       * silently seeding nothing. */}
      {showSeedOptions && (
        <Stack direction="row" spacing={2} sx={{ mt: 1.5, flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
          <FormControlLabel
            control={<Switch checked={seed} onChange={(e) => setSeed(e.target.checked)} />}
            label={<Typography variant="body2">Also seed this tenant</Typography>}
          />
          <Collapse in={seed} orientation="horizontal">
            <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1 }}>
              <TextField select size="small" label="Seed scale" value={seedScale}
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
          {view === 'automated' && !result.ok
            && result.phases.some((p) => p.status === 'failed'
              && (p.name.startsWith('provision Cloud project')
                || p.name.startsWith('authenticate gcloud'))) && (
            <Alert severity="warning" sx={{ mt: 1 }}
              action={onRequestManual && (
                <Button color="inherit" size="small" onClick={onRequestManual}>
                  Go to Manual
                </Button>
              )}
            >
              Couldn't finish setting up the Cloud project automatically
              (see the failed step above). Upload a service-account key on
              the Manual tab instead (from running provision_gcp.py on your
              own machine) — after that, signing in only needs to grant
              delegation, not create the project.
            </Alert>
          )}

          {setUpOk && (
            <TenantInventoryPanel inv={inv} busy={invBusy} error={invError}
                                  domain={domain} scanProgress={scanProgress}
                                  ageSeconds={scanAge}
                                  onRefresh={() => loadInventory(false, true)}
                                  onDeepScan={runDeepScan} />
          )}
        </Box>
      )}

      {/* Re-provisioning needs a password, so it reuses the same sign-in
          dialog and Reason Code the ordinary launch does -- one credential
          path, not a second one that could drift from it. */}
      {setUpOk && (
        <ReprovisionPanel
          side={side} domain={domain} busy={busy || !!status?.running}
          onReprovision={(scopes) => {
            reprovisionRef.current = { scopes }
            if (password) setAsk(true)
            else setAuthDialogOpen(true)
          }}
        />
      )}

      {setUpOk && showSeedOptions && side === 'source' && (
        <Box sx={{ mt: 2, pt: 2, borderTop: '1px solid', borderColor: 'divider' }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
            Setup done — seed it
          </Typography>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
            <TextField select size="small" label="Seed scale" value={seedScale}
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
            <TextField
              size="small" label="Only these users" placeholder="george, ivan"
              value={seedUsers}
              onChange={(e) => setSeedUsers(e.target.value)}
              inputProps={{ 'data-testid': 'quick-seed-users' }}
              sx={{ width: 220 }}
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
            "Only these users" takes localparts, not addresses — blank seeds
            every user the tenant has, which on a real tenant is days rather
            than minutes. Name a few when you are checking a change.
          </Typography>
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

          {/* Step one, and the one that was missing entirely.
              Auto-mapping pairs only accounts that already exist on BOTH
              tenants, and provisioning only creates accounts already mapped
              -- so on a fresh target neither could start the other. A target
              holding one account mapped one of the source's 201 users and
              then correctly reported nothing to create, which read as the
              button doing nothing. */}
          <Stack direction="row" spacing={2} sx={{ mb: 1.5, flexWrap: 'wrap',
                                                   gap: 1, alignItems: 'center' }}>
            <Button variant="outlined" startIcon={<AddUsersIcon />}
                    disabled={postBusy || identityStatus?.running}
                    data-testid="build-identity-map"
                    onClick={() => setPostAction('mapUsers')}>
              {identityStatus?.running ? 'Mapping…' : 'Map & create users from source'}
            </Button>
            <Typography variant="caption" color="text.secondary"
                        data-testid="identity-count">
              {identityStatus
                ? `${identityStatus.mapped.toLocaleString()} user(s) mapped`
                : 'checking…'}
            </Typography>
          </Stack>

          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
            <Button variant="contained" startIcon={<SeedIcon />}
                    disabled={postBusy || provisionStatus?.running}
                    onClick={() => setPostAction('provision')}>
              {provisionStatus?.running ? 'Running…' : 'Provision users now'}
            </Button>
            {provisionStatus && (provisionStatus.created || provisionStatus.total) && (
              <Typography variant="caption" color="text.secondary"
                          data-testid="provision-counts">
                {provisionStatus.created}/{provisionStatus.total} created
                {provisionStatus.existing
                  ? `, ${provisionStatus.existing} already existed` : ''}
                {provisionStatus.failed ? `, ${provisionStatus.failed} failed` : ''}
              </Typography>
            )}
          </Stack>

          {/* The accounts themselves, not just a fraction.
              "0/1 created" on a tenant whose only user already exists is
              indistinguishable from "nothing happened" -- which is exactly
              how this read when it was clicked repeatedly and appeared to do
              nothing. Naming the address and its state is what makes
              "already there" legible as a result. */}
          {provisionStatus?.users && provisionStatus.users.length > 0 && (
            <Box sx={{ mt: 1.5 }} data-testid="provision-users">
              {provisionStatus.running && (
                <LinearProgress
                  variant={provisionStatus.total ? 'determinate' : 'indeterminate'}
                  value={provisionStatus.total
                    ? Math.min(100, Math.round(
                        100 * provisionStatus.users.length / provisionStatus.total))
                    : undefined}
                  sx={{ mb: 1, borderRadius: 1 }} />
              )}
              <Box sx={{ maxHeight: 200, overflowY: 'auto', border: '1px solid',
                         borderColor: 'divider', borderRadius: 1, p: 1 }}>
                {provisionStatus.users.map((u) => (
                  <Stack key={u.email} direction="row" spacing={1}
                         alignItems="center" sx={{ py: 0.25 }}
                         data-testid={`prov-${u.email}`}>
                    <Chip size="small" variant="outlined"
                          color={u.state === 'created' ? 'success'
                                 : u.state === 'failed' ? 'error' : 'default'}
                          label={u.state === 'existing' ? 'already there' : u.state} />
                    <Typography variant="body2" sx={{ fontSize: 12 }}>
                      {u.email}
                    </Typography>
                    {u.detail && (
                      <Typography variant="caption" color="error.main">
                        {u.detail}
                      </Typography>
                    )}
                  </Stack>
                ))}
              </Box>
            </Box>
          )}
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
              opens a browser, signs in as <strong>{email || 'the admin'}</strong>{' '}
              to create the Cloud project under their own Google account,
              then continues straight into domain-wide delegation — nothing
              to run or upload separately.{' '}
              {showSeedOptions && seed && 'Also seeds this tenant with test data. '}
              {showProvisionUsers && provisionUsers && 'Also creates target accounts. '}
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
