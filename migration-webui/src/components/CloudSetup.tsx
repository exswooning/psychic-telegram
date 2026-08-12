import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Collapse, Divider,
  FormControlLabel, Paper, Stack, Switch, TextField, Typography,
} from '@mui/material'
import {
  Cloud as CloudIcon, ContentCopy as CopyIcon,
  ExpandMore, ExpandLess,
} from '@mui/icons-material'
import {
  GcpStatus, startGcpProvision, fetchGcpStatus, enableApis,
} from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * The Cloud side of setup, as a button rather than a runbook.
 *
 * Creating the GCP half of a migration by hand means: find the org id,
 * create two projects, enable nine APIs on each (individually -- the
 * batched call fails on a fresh project), create two service accounts,
 * grant each one serviceusage so the tool can later repair its own API
 * enablement, override an org policy that silently blocks key creation,
 * then download two keys. Every one of those steps has a failure mode that
 * looks like success from the outside, which is exactly why it belongs in
 * code rather than in a document someone follows at 2am.
 *
 * Domain-wide delegation is the one step that stays manual, because Google
 * publishes no API for it. What this can do is hand over the exact client
 * IDs the DWD panel needs, so the two halves join up without a copy-paste
 * hunt across two consoles.
 *
 * Defaults to a dry run. Provisioning creates real billable resources in
 * the operator's organisation and writes credentials to disk; the safe
 * option should be the one you get by not thinking about it.
 */
const CloudSetup: React.FC = () => {
  const [status, setStatus] = useState<GcpStatus | null>(null)
  const [sourceDomain, setSourceDomain] = useState('')
  const [targetDomain, setTargetDomain] = useState('')
  const [orgId, setOrgId] = useState('')
  const [dryRun, setDryRun] = useState(true)
  const [ask, setAsk] = useState<null | 'provision' | 'apis'>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = useCallback(() => {
    fetchGcpStatus().then(setStatus).catch(() => {})
  }, [])

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

  const doProvision = async (reason: string) => {
    setBusy(true); setError(null); setMsg(null)
    try {
      const r = await startGcpProvision(reason, sourceDomain.trim(),
                                        targetDomain.trim(), orgId.trim(), dryRun)
      setMsg(r.detail); setAsk(null); setExpanded(true); poll()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const doEnableApis = async (reason: string) => {
    setBusy(true); setError(null); setMsg(null)
    try {
      const r = await enableApis(reason)
      setMsg(r.detail); setAsk(null)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const copy = (text: string, tag: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(tag)
      setTimeout(() => setCopied(null), 2000)
    })
  }

  const result = status?.result
  const canProvision = sourceDomain.trim() && targetDomain.trim() && !status?.running

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1}
             sx={{ mb: 1, flexWrap: 'wrap', gap: 1 }}>
        <CloudIcon color="action" />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>Cloud setup</Typography>
        {status?.running && (
          <Chip size="small" color="primary" icon={<CircularProgress size={10} />}
                label="provisioning…" />
        )}
        <Button size="small" onClick={() => setExpanded((v) => !v)}
                endIcon={expanded ? <ExpandLess /> : <ExpandMore />}>
          {expanded ? 'Hide' : 'Show'}
        </Button>
      </Stack>

      <Typography variant="caption" color="text.secondary">
        Creates both GCP projects, enables every API the engines use, makes
        the service accounts and downloads their keys. Safe to re-run — each
        step skips what already exists.
      </Typography>

      <Collapse in={expanded}>
        <Box sx={{ mt: 2 }}>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <TextField size="small" label="Source domain" value={sourceDomain}
                       onChange={(e) => setSourceDomain(e.target.value)}
                       placeholder="c.example.com"
                       sx={{ width: { xs: '100%', sm: 220 } }} />
            <TextField size="small" label="Target domain" value={targetDomain}
                       onChange={(e) => setTargetDomain(e.target.value)}
                       placeholder="a.example.com"
                       sx={{ width: { xs: '100%', sm: 220 } }} />
            <TextField size="small" label="Org ID (optional)" value={orgId}
                       onChange={(e) => setOrgId(e.target.value)}
                       helperText="auto-detected if you own exactly one"
                       sx={{ width: { xs: '100%', sm: 220 } }} />
          </Stack>

          <FormControlLabel
            sx={{ mt: 1 }}
            control={<Switch checked={dryRun}
                             onChange={(e) => setDryRun(e.target.checked)} />}
            label={
              <Typography variant="body2">
                Dry run — show every step without creating anything
              </Typography>
            }
          />

          <Stack direction="row" spacing={2} sx={{ mt: 1, flexWrap: 'wrap', gap: 2 }}>
            <Button variant="contained" disabled={!canProvision}
                    onClick={() => setAsk('provision')}>
              {status?.running ? 'Running…'
                : dryRun ? 'Preview provisioning' : 'Provision Cloud setup'}
            </Button>
            <Button variant="outlined" disabled={status?.running}
                    onClick={() => setAsk('apis')}>
              Enable missing APIs
            </Button>
          </Stack>

          {msg && <Alert severity="success" sx={{ mt: 2 }}
                         onClose={() => setMsg(null)}>{msg}</Alert>}
          {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}

          {result?.error && (
            <Alert severity="error" sx={{ mt: 2 }}>{result.error}</Alert>
          )}

          {result?.sides?.length ? (
            <Box sx={{ mt: 2 }}>
              <Divider sx={{ mb: 1.5 }} />
              <Typography variant="caption" color="text.secondary">
                account {result.account} · org {result.org || '(none)'}
              </Typography>
              {result.sides.map((side) => (
                <Box key={side.side} sx={{ mt: 1.5 }}>
                  <Stack direction="row" spacing={1} alignItems="center"
                         sx={{ flexWrap: 'wrap', gap: 1 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                      {side.side} — {side.project}
                    </Typography>
                    <Chip size="small" label={side.ok ? 'ok' : 'failed'}
                          color={side.ok ? 'success' : 'error'}
                          variant={side.ok ? 'outlined' : 'filled'} />
                  </Stack>
                  <Box component="pre" sx={{
                    fontSize: 11, p: 1.5, mt: 0.5, bgcolor: 'action.hover',
                    borderRadius: 1, overflowX: 'auto', maxHeight: 220,
                    whiteSpace: 'pre-wrap', m: 0,
                  }}>
                    {side.steps.map((s) =>
                      `${s.status === 'ok' ? 'ok  '
                        : s.status === 'failed' ? 'FAIL'
                        : '--  '} ${s.name}${s.detail ? '  ' + s.detail : ''}`
                    ).join('\n')}
                  </Box>
                  {side.clientId && (
                    <Stack direction="row" spacing={1} alignItems="center"
                           sx={{ mt: 0.5, flexWrap: 'wrap', gap: 1 }}>
                      <Typography variant="caption"
                                  sx={{ fontFamily: 'ui-monospace, monospace' }}>
                        client ID {side.clientId}
                      </Typography>
                      <Button size="small" startIcon={<CopyIcon />}
                              onClick={() => copy(
                                `python3 dwd_helper.py --tenant ${side.side} `
                                + `--client-id ${side.clientId}`, side.side)}>
                        {copied === side.side ? 'Copied' : 'Copy DWD command'}
                      </Button>
                    </Stack>
                  )}
                </Box>
              ))}
              <Alert severity="info" sx={{ mt: 2 }}>
                Domain-wide delegation is the only step with no API — a super
                admin has to authorise these client IDs. Use the copied
                command, then re-check the Domain-Wide Delegation panel above.
              </Alert>
            </Box>
          ) : null}
        </Box>
      </Collapse>

      <ReasonCodeDialog
        open={ask === 'provision'} busy={busy} error={error}
        destructive={!dryRun}
        confirmPhrase={dryRun ? undefined : 'PROVISION'}
        title={dryRun ? 'Preview Cloud provisioning' : 'Provision Cloud setup'}
        description={
          dryRun ? (
            <>Lists every step it would take. Creates nothing, changes
            nothing, writes no files.</>
          ) : (
            <>
              Creates two <strong>real GCP projects</strong> under your
              organisation, enables nine APIs on each, creates two service
              accounts and <strong>downloads their private keys</strong> to{' '}
              <code>keys/</code>. It also relaxes the{' '}
              <code>disableServiceAccountKeyCreation</code> org policy on
              those two projects only. Existing projects and keys are reused,
              never overwritten.
            </>
          )
        }
        onCancel={() => { setAsk(null); setError(null) }}
        onConfirm={doProvision}
      />

      <ReasonCodeDialog
        open={ask === 'apis'} busy={busy} error={error}
        title="Enable missing Cloud APIs"
        description={
          <>
            Switches on any required API that is off, on both tenants'
            projects. This is the common repair when a service is turned on
            later — a granted DWD scope does <strong>not</strong> enable the
            API behind it, and the two fail with completely different errors.
            Needs the service account to hold{' '}
            <code>serviceusage.serviceUsageAdmin</code>.
          </>
        }
        onCancel={() => { setAsk(null); setError(null) }}
        onConfirm={doEnableApis}
      />
    </Paper>
  )
}

export default CloudSetup
