import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Collapse, Paper, Stack,
  Typography,
} from '@mui/material'
import {
  VpnKey as KeyIcon, CheckCircle as OkIcon, Error as ErrIcon,
  ContentCopy as CopyIcon, ExpandMore, ExpandLess,
} from '@mui/icons-material'
import { DwdStatus, fetchDwdStatus } from '@/api/controlPlane'

/**
 * Domain-Wide Delegation setup, front and centre.
 *
 * Google gives no API to grant DWD or to read what is currently granted --
 * a super admin has to click it through in the Admin console, and the only
 * way to know whether it worked is to try using it. That is exactly what
 * this panel does: verify_scopes.py mints a token per required scope and
 * reports which ones Google actually issues. Nothing here can fix a
 * missing scope by itself (dwd_helper.py drives a browser, which needs a
 * local display this dashboard does not have) -- what it CAN do is tell an
 * operator, at a glance, whether every other panel on this page is about
 * to work or about to fail with unauthorized_client.
 *
 * Shown at the top of Mission Control, and collapsed automatically once
 * both tenants report fully delegated -- a permanently-open setup panel on
 * a working install is exactly the kind of ignorable green box that let
 * this project's own SEED_SCOPES gap go unnoticed for as long as it did.
 */
const DwdSetup: React.FC = () => {
  const [source, setSource] = useState<DwdStatus | null>(null)
  const [target, setTarget] = useState<DwdStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)

  const refresh = useCallback(() => {
    setLoading(true)
    Promise.all([fetchDwdStatus('source'), fetchDwdStatus('target')])
      .then(([s, t]) => { setSource(s); setTarget(t) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const complete = (s: DwdStatus | null) => s?.checked && (s.missing?.length ?? 1) === 0
  const bothComplete = complete(source) && complete(target)

  useEffect(() => {
    // Auto-expand the first time either tenant is incomplete; never
    // re-expand on its own after that so a manual collapse sticks.
    if (source && target && !bothComplete) setExpanded(true)
  }, [source, target, bothComplete])

  const copyCommand = (tenant: 'source' | 'target', status: DwdStatus) => {
    const missing = status.missing ?? []
    const scopes = missing.length ? missing.join(',') : '<all required scopes>'
    const cmd = `python3 dwd_helper.py --tenant ${tenant} `
      + `--client-id ${status.clientId || '<client id>'} --scopes "${scopes}"`
    navigator.clipboard.writeText(cmd).then(() => {
      setCopied(tenant)
      setTimeout(() => setCopied(null), 2000)
    })
  }

  const Row: React.FC<{ label: string; tenant: 'source' | 'target'; status: DwdStatus | null }> =
    ({ label, tenant, status }) => {
      if (!status) return null
      if (!status.checked) {
        return (
          <Alert severity="warning" sx={{ mb: 1 }}>
            {label}: could not check ({status.error})
          </Alert>
        )
      }
      const ok = (status.missing?.length ?? 1) === 0
      return (
        <Box sx={{ mb: 1.5 }}>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>
            {ok ? <OkIcon color="success" fontSize="small" />
                : <ErrIcon color="error" fontSize="small" />}
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{label}</Typography>
            <Chip size="small" variant="outlined"
                  label={`${status.live}/${status.total} scopes`}
                  color={ok ? 'success' : 'warning'} />
            {!ok && (
              <Button size="small" startIcon={<CopyIcon />}
                      onClick={() => copyCommand(tenant, status)}>
                {copied === tenant ? 'Copied' : 'Copy fix command'}
              </Button>
            )}
          </Stack>
          {!ok && (
            <Typography variant="caption" color="text.secondary" sx={{
              display: 'block', mt: 0.5, fontFamily: 'ui-monospace, monospace',
            }}>
              missing: {status.missing!.map((m) => m.split('/').pop()).join(', ')}
            </Typography>
          )}
        </Box>
      )
    }

  return (
    <Paper variant="outlined" sx={{
      borderRadius: 2, p: 2, mb: 3,
      borderColor: bothComplete ? 'divider' : 'warning.main',
    }}>
      <Stack direction="row" alignItems="center" spacing={1}
             sx={{ cursor: 'pointer' }}
             onClick={() => setExpanded((v) => !v)}>
        <KeyIcon color={bothComplete ? 'action' : 'warning'} />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>Domain-Wide Delegation</Typography>
        {loading && <CircularProgress size={16} />}
        {!loading && bothComplete && (
          <Chip size="small" color="success" variant="outlined" label="fully delegated" />
        )}
        {!loading && !bothComplete && (
          <Chip size="small" color="warning" label="setup needed" />
        )}
        <Button size="small" onClick={(e) => { e.stopPropagation(); refresh() }}>
          Recheck
        </Button>
        {expanded ? <ExpandLess /> : <ExpandMore />}
      </Stack>

      <Collapse in={expanded}>
        <Box sx={{ mt: 2 }}>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
            Google has no API to grant or read a delegation entry -- this
            checks by minting a token for each scope the tool needs and
            seeing which ones Google actually issues. A missing scope fails
            the whole feature it belongs to with{' '}
            <code>unauthorized_client</code>, not a partial result.
          </Typography>
          <Row label="Source tenant" tenant="source" status={source} />
          <Row label="Target tenant" tenant="target" status={target} />
          {!bothComplete && (
            <Alert severity="info" sx={{ mt: 1 }}>
              Run the copied command on a machine with a browser and a
              display (dwd_helper.py cannot run headless on this host) --
              it signs in, walks the Admin console, and grants exactly the
              missing scopes without touching what is already delegated.
              Needs <code>pip install playwright &amp;&amp; playwright install
              chromium</code> once.
            </Alert>
          )}
        </Box>
      </Collapse>
    </Paper>
  )
}

export default DwdSetup
