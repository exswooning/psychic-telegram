import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Collapse,
  FormControlLabel, MenuItem, Paper, Stack, Switch, TextField, Typography,
} from '@mui/material'
import { RocketLaunch as QuickIcon, Grass as SeedIcon } from '@mui/icons-material'
import {
  FullSetupStatus, startFullSetup, fetchFullSetupStatus,
  startProvision, fetchProvisionStatus, ProvisionStatus,
} from '@/api/controlPlane'
import { runSeed } from '@/api/client'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * Domain, admin email, admin password. One button. Everything else --
 * project, APIs, service account, key, delegation, verification, and
 * optionally seeding or user creation -- happens server-side via
 * full_setup.py.
 *
 * The honest limit, stated rather than hidden: this drives a REAL browser
 * through Google's sign-in to grant delegation, which needs a display and
 * sometimes a human for 2FA or a captcha. It only works where the control
 * plane process itself is running with gcloud and a browser available --
 * pointed at a headless host it fails cleanly with that exact explanation,
 * not a stuck spinner. The step-by-step panels below this one are the
 * fallback for whenever that human-in-the-loop moment happens: they show
 * the same client ID this produces, ready to run dwd_helper.py by hand and
 * watch the browser directly.
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

  // Standalone "do it now" actions, separate from the setup dialog above --
  // these run after Quick Setup has already succeeded, and hit the same
  // password-free endpoints the step-by-step panels below already use
  // (webui.py's /api/seed, main.py provision-users), not a re-run of
  // full_setup.py. Re-running full_setup.py to seed would force a second,
  // unnecessary browser-based DWD sign-in for something that needs neither
  // a browser nor a password.
  const [postAction, setPostAction] = useState<'seed' | 'provision' | null>(null)
  const [postBusy, setPostBusy] = useState(false)
  const [postError, setPostError] = useState<string | null>(null)
  const [postDone, setPostDone] = useState<string | null>(null)
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
      await startFullSetup(reason, side, domain.trim(), email.trim(), password, {
        orgId: orgId.trim(), dryRun,
        seed: showSeedOptions ? seed : false, seedScale,
        createUsers: showSeedOptions ? createUsers : false,
        provisionUsers: showProvisionUsers ? provisionUsers : false,
      })
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
        const r = await runSeed(domain.trim(), seedScale, createUsers, false)
        if (!r.ok) throw new Error(r.error || 'seed failed')
        setPostDone('seed complete')
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

      <Typography variant="caption" color="text.secondary">
        Domain and admin credentials in, a fully delegated tenant out.
        Needs a display for the sign-in step — if it stalls waiting on 2FA
        or a captcha, use the step-by-step panels below to watch the
        browser directly.
      </Typography>

      <Stack direction="row" spacing={2} sx={{ mt: 2, flexWrap: 'wrap', gap: 2 }}>
        <TextField size="small" label={`${side} domain`} value={domain}
                   onChange={(e) => setDomain(e.target.value)}
                   placeholder={side === 'source' ? 'c.example.com' : 'a.example.com'}
                   sx={{ width: { xs: '100%', sm: 200 } }} />
        <TextField size="small" label="Super admin email" value={email}
                   onChange={(e) => setEmail(e.target.value)}
                   placeholder={`admin@${domain || 'example.com'}`}
                   sx={{ width: { xs: '100%', sm: 220 } }} />
        <TextField size="small" label="Admin password" type="password"
                   value={password} onChange={(e) => setPassword(e.target.value)}
                   autoComplete="off"
                   helperText="sent once, never stored"
                   sx={{ width: { xs: '100%', sm: 200 } }} />
        <TextField size="small" label="Org ID (optional)" value={orgId}
                   onChange={(e) => setOrgId(e.target.value)}
                   sx={{ width: { xs: '100%', sm: 160 } }} />
      </Stack>

      <FormControlLabel
        sx={{ mt: 1, display: 'block' }}
        control={<Switch checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />}
        label={<Typography variant="body2">Dry run first (recommended)</Typography>}
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

      <Button variant="contained" sx={{ mt: 2 }} disabled={!canLaunch}
              onClick={() => setAsk(true)}>
        {status?.running ? 'Running…' : dryRun ? 'Preview' : `Set up ${side}`}
      </Button>
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
              `${p.status === 'ok' ? 'ok  ' : p.status === 'failed' ? 'FAIL' : '--  '} `
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
                    disabled={postBusy || !domain.trim()}
                    onClick={() => setPostAction('seed')}>
              Seed now
            </Button>
          </Stack>
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
            <>Lists every step without creating anything or opening a
            browser.</>
          ) : (
            <>
              Creates a <strong>real GCP project</strong>, enables APIs,
              creates a service account and downloads its key, then opens a
              browser and signs in as <strong>{email || 'the admin'}</strong> to
              grant domain-wide delegation.{' '}
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
        open={postAction !== null} busy={postBusy} error={postError}
        destructive={postAction === 'seed'}
        confirmPhrase={postAction === 'seed' ? 'SEED' : undefined}
        title={postAction === 'seed' ? `Seed ${domain || 'the source tenant'}`
                                      : 'Provision target accounts'}
        description={
          postAction === 'seed' ? (
            <>Writes test data into <strong>{domain || 'the source tenant'}</strong>.
            No password needed — uses the service account key from setup.</>
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
